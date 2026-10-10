"""Tests for the model scout: the agentic form-guide builder.

No LLM or network in here: `_synthesize` and `_search` are patched, so what is
exercised is the pipeline — which arms are picked, when a dossier is rewritten,
when a search is actually spent, and how the result lands in `model_dossiers`.
"""
import json

import pytest

from mininfer import scout as scout_mod
from mininfer.fetch import utcnow as _utcnow
from mininfer.store import Store


def _seed(store: Store):
    store.conn.execute("INSERT INTO weights (weights_id, display_name, benchmark, modalities)"
                       " VALUES ('hf:m','M','{\"coding\": 60}','[\"text\"]')")
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, context_window, zero_price, status, pricing_state)"
        " VALUES ('openrouter:m','hf:m','openrouter','m',0.0,0.0,1000,1,'live','free')")
    # A paid arm: must NOT be scouted.
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, zero_price, status)"
        " VALUES ('openrouter:p','hf:m','openrouter','p',1.0,2.0,0,'live')")
    store.commit()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "s.db")
    _seed(s)
    return s


def _fake_synth(agent_model, prompt):
    return {"core_competency": "core", "summary": "sum",
            "warrior": "The Test Blade", "story": "A story of one task.",
            "strengths": ["s"], "weaknesses": ["w"],
            "when_to_use": ["general_chat"], "when_not_to_use": ["x"],
            "best_for": ["general_chat"], "confidence": 0.9}


def test_free_deployments_only_free_arms(store):
    rows = store.free_deployments()
    assert [r["deploy_id"] for r in rows] == ["openrouter:m"]
    assert rows[0]["display_name"] == "M"
    assert rows[0]["benchmark"] == '{"coding": 60}'


def test_outcome_stats_aggregates(store):
    store.observe("openrouter:m", "general_chat", True, ts="2026-10-08T00:00:00+00:00",
                  latency_ms=100.0)
    store.observe("openrouter:m", "general_chat", False, ts="2026-10-08T00:00:01+00:00",
                  error_class="429")
    st = store.outcome_stats("openrouter:m")
    assert st["n"] == 2 and st["wins"] == 1 and st["n_429"] == 1
    assert store.outcome_stats("missing") is None


def test_dossier_round_trip(store):
    store.upsert_dossier("openrouter:m", weights_id="hf:m", display_name="M",
                         provider="openrouter", price_out=0.0,
                         core_competency="core", summary="sum",
                         strengths=["s"], weaknesses=["w"], when_to_use=["t"],
                         when_not_to_use=["n"], best_for=["t"],
                         search_sources=[{"title": "q", "url": "u"}],
                         facts={"k": 1}, confidence=0.5,
                         generated_at="2026-10-08T00:00:00+00:00")
    rows = store.dossiers()
    assert len(rows) == 1 and rows[0]["core_competency"] == "core"
    assert store.dossier("openrouter:m")["summary"] == "sum"
    # substring filter hits display_name
    assert len(store.dossiers(substr="M")) == 1
    assert len(store.dossiers(substr="nope")) == 0


def test_scout_writes_and_skips_fresh(store, monkeypatch):
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))

    rep = scout_mod.scout(store, search_enabled=False, task_names=["general_chat"])
    assert rep["written"] == 1 and rep["errors"] == 0
    d = store.dossier("openrouter:m")
    assert d["core_competency"] == "core"
    # The storied half round-trips beside the factual fields.
    assert d["warrior"] == "The Test Blade" and d["story"] == "A story of one task."

    # Fresh dossier: a second pass skips it, a forced one rewrites it.
    again = scout_mod.scout(store, search_enabled=False, task_names=["general_chat"])
    assert again["written"] == 0 and again["skipped_fresh"] == 1
    forced = scout_mod.scout(store, search_enabled=False, force=True,
                             task_names=["general_chat"])
    assert forced["written"] == 1


def test_a_dossier_missing_its_story_is_re_synthesised_without_a_search(store, monkeypatch):
    """An upgrade path: old dossiers gain their warrior from their own facts."""
    store.upsert_dossier("openrouter:m", weights_id="hf:m", display_name="M",
                         provider="openrouter", price_out=0.0, core_competency="core",
                         summary="sum", strengths=[], weaknesses=[], when_to_use=[],
                         when_not_to_use=[], best_for=[], search_sources=[],
                         # Facts that still match the deployment: only the missing
                         # story should mark it stale, not a facts change.
                         facts={"status": "live", "pricing_state": "free",
                                "price_out": 0.0},
                         confidence=0.5,
                         generated_at=_utcnow())
    store.commit()
    searched: list[str] = []
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search",
                        lambda s, d, e, **k: searched.append(d["deploy_id"]) or ([], {}))
    rep = scout_mod.scout(store, search_enabled=True, task_names=["general_chat"])
    assert rep["written"] == 1 and rep["skipped_fresh"] == 0
    assert searched == []                       # facts were sufficient, no search
    assert store.dossier("openrouter:m")["warrior"] == "The Test Blade"


def test_changing_the_voice_re_voices_the_dossier_without_a_search(store, monkeypatch):
    """`--tone deadpool` must actually rewrite — a dossier with a warrior still
    looks fresh, so the recorded tone is what makes the change detectable."""
    store.upsert_dossier("openrouter:m", weights_id="hf:m", display_name="M",
                         provider="openrouter", price_out=0.0, core_competency="core",
                         summary="sum", warrior="The Old Voice", story="old",
                         strengths=[], weaknesses=[], when_to_use=[],
                         when_not_to_use=[], best_for=[], search_sources=[],
                         facts={"status": "live", "pricing_state": "free",
                                "price_out": 0.0, "scout_tone": "epic"},
                         confidence=0.5,
                         generated_at=_utcnow())
    store.commit()
    searched: list[str] = []
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search",
                        lambda s, d, e, **k: searched.append(d["deploy_id"]) or ([], {}))

    # Same tone -> still fresh, skipped.
    monkeypatch.setenv("MI_SCOUT_TONE", "epic")
    assert scout_mod.scout(store, search_enabled=True,
                           task_names=["general_chat"])["skipped_fresh"] == 1

    # New tone -> rewritten, no search spent.
    monkeypatch.setenv("MI_SCOUT_TONE", "deadpool")
    rep = scout_mod.scout(store, search_enabled=True, task_names=["general_chat"])
    assert rep["written"] == 1 and rep["skipped_fresh"] == 0
    assert searched == []
    assert store.dossier("openrouter:m")["warrior"] == "The Test Blade"


def test_scout_skips_deprecated(store, monkeypatch):
    store.conn.execute("UPDATE deployments SET status='deprecated' WHERE deploy_id='openrouter:m'")
    store.commit()
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))
    rep = scout_mod.scout(store, search_enabled=False, force=True,
                          task_names=["general_chat"])
    assert rep["scanned"] == 0 and rep["written"] == 0


def test_the_dossier_voice_is_configurable(monkeypatch):
    facts = {"display_name": "X"}
    monkeypatch.delenv("MI_SCOUT_TONE", raising=False)
    assert "Epic-fantasy" in scout_mod._prompt(facts, [], ["t"])   # default
    monkeypatch.setenv("MI_SCOUT_TONE", "deadpool")
    assert "Deadpool" in scout_mod._prompt(facts, [], ["t"])
    monkeypatch.setenv("MI_SCOUT_TONE", "plain")
    assert "No metaphor" in scout_mod._prompt(facts, [], ["t"])


def test_scout_search_uses_tinyfish_by_default(store, monkeypatch):
    """TinyFish only: structured, citable research, no silent thinner tier."""
    captured: dict = {}

    def fake_search(query, **kw):
        captured.update(kw)
        from mininfer.search import SearchResult
        return SearchResult(query=query, provider=kw.get("provider", "?"), results=[])

    monkeypatch.setattr(scout_mod.search_mod, "search", fake_search)
    monkeypatch.delenv("MI_SCOUT_SEARCH_PROVIDER", raising=False)
    deploy = store.free_deployments()[0]
    scout_mod._search(store, deploy, True)
    assert captured.get("provider") == "tinyfish"


def test_scout_search_provider_is_overridable(store, monkeypatch):
    captured: dict = {}

    def fake_search(query, **kw):
        captured.update(kw)
        from mininfer.search import SearchResult
        return SearchResult(query=query, provider=kw.get("provider", "?"), results=[])

    monkeypatch.setattr(scout_mod.search_mod, "search", fake_search)
    monkeypatch.setenv("MI_SCOUT_SEARCH_PROVIDER", "duckduckgo")
    deploy = store.free_deployments()[0]
    scout_mod._search(store, deploy, True)
    assert captured.get("provider") == "duckduckgo"


def test_scout_dry_run_writes_nothing(store, monkeypatch):
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))
    rep = scout_mod.scout(store, search_enabled=False, dry_run=True,
                          task_names=["general_chat"])
    assert rep["written"] == 1
    assert store.dossier("openrouter:m") is None


def test_search_is_spent_on_the_first_dossier(store, monkeypatch):
    """A new arm has no snapshot to re-summarise, so a search is warranted."""
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    searched: list[str] = []
    monkeypatch.setattr(scout_mod, "_search",
                        lambda s, d, e: searched.append(d["deploy_id"]) or
                        ([{"title": "q", "url": "u"}], {"enabled": True}))
    scout_mod.scout(store, search_enabled=True, task_names=["general_chat"])
    assert searched == ["openrouter:m"]
    facts = json.loads(store.dossier("openrouter:m")["facts"])
    assert facts["search"]["enabled"] is True


def test_search_is_decomposed_away_when_facts_suffice(store, monkeypatch):
    """Unchanged facts + existing evidence -> no new search is spent."""
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))
    scout_mod.scout(store, search_enabled=False, task_names=["general_chat"])

    searched: list[str] = []
    monkeypatch.setattr(scout_mod, "_search",
                        lambda s, d, e: searched.append(d["deploy_id"]) or ([], {}))
    rep = scout_mod.scout(store, search_enabled=True, max_age_hours=0,
                          task_names=["general_chat"])
    assert rep["written"] == 1 and searched == []
    facts = json.loads(store.dossier("openrouter:m")["facts"])
    assert facts["search"]["skipped"] == "facts_sufficient"


def test_a_changed_status_forces_a_research(store, monkeypatch):
    """The dossier is rewritten *and* re-searched when the registry moved under it."""
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))
    scout_mod.scout(store, search_enabled=False, task_names=["general_chat"])

    store.conn.execute("UPDATE deployments SET status='hibernated',"
                       " status_reason='was free, now paid' WHERE deploy_id='openrouter:m'")
    store.commit()

    searched: list[str] = []
    monkeypatch.setattr(scout_mod, "_search",
                        lambda s, d, e: searched.append(d["deploy_id"]) or ([], {}))
    rep = scout_mod.scout(store, search_enabled=True, max_age_hours=1000,
                          task_names=["general_chat"])
    assert rep["skipped_fresh"] == 0 and rep["written"] == 1
    assert searched == ["openrouter:m"]
    facts = json.loads(store.dossier("openrouter:m")["facts"])
    assert facts["status"] == "hibernated"


def test_only_new_scouts_arms_without_a_dossier(store, monkeypatch):
    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))
    scout_mod.scout(store, search_enabled=False, task_names=["general_chat"])
    assert store.dossier("openrouter:m") is not None

    # A second free arm arrives via ingest.
    store.conn.execute(
        "INSERT INTO deployments (deploy_id, weights_id, provider, provider_model_id,"
        " price_in, price_out, zero_price, status) VALUES"
        " ('openrouter:new','hf:m','openrouter','new',0.0,0.0,1,'live')")
    store.commit()

    monkeypatch.setattr(scout_mod, "_synthesize", _fake_synth)
    monkeypatch.setattr(scout_mod, "_search", lambda s, d, e: ([], {"enabled": False}))
    seen: list[str] = []
    rep = scout_mod.scout(store, search_enabled=False, only_new=True,
                          task_names=["general_chat"],
                          on_arm=lambda d: seen.append(d["deploy_id"]))
    assert seen == ["openrouter:new"]
    assert rep["written"] == 1
