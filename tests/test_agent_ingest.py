"""Tests for the LangGraph ingestion agents (Phase 5).

The graph is tested end-to-end offline: `_fetch_body` and `_extract_facts` are
stubbed, so the whole fetch -> extract -> normalize -> validate -> commit/quarantine
path runs without a network or an LLM. What must not drift:

  * a scraped fact is only written when it survives validation;
  * a price that disagrees with the registry is quarantined, not merged.
"""
from __future__ import annotations

import sqlite3

import os

import pytest

# The agent graph is the optional `[agents]` extra (langgraph + bs4). Skip when
# it is absent instead of failing collection: `pip install -e .` — the README's
# install — must still be able to run the rest of the suite.
pytest.importorskip("bs4")
pytest.importorskip("langgraph")

import mininfer.agent_ingest as ai
from mininfer.store import Store


HTML = (
    "<html><body><h1>Acme model catalogue</h1>"
    "<p>Qwen3-30B-A3B: $0.05 in / $0.15 out per million tokens, 32K context.</p>"
    "<table><tr><th>Model</th><th>Score</th></tr>"
    "<tr><td>Qwen3-30B-A3B</td><td>60</td></tr></table></body></html>"
)

FACTS = {
    "source_type": "pricing",
    "models": [{
        "model_name": "Qwen3-30B-A3B", "provider": "acme",
        "provider_model_id": "acme/qwen3-30b",
        "price_in_usd_per_mtok": 0.05, "price_out_usd_per_mtok": 0.15,
        "free": False, "context_window": 32000,
        "tools": True, "structured": None,
        "benchmarks": [{"name": "aa_intelligence", "score": 60.0}],
        "confidence": 0.7,
    }],
}


# ------------------------------------------------------------ html extraction


def test_html_extract_returns_text_and_tables():
    text, tables = ai._html_extract(HTML)
    assert "Qwen3-30B-A3B" in text
    assert any("Score" in cell
               for table in tables for row in table for cell in row)


def test_looks_like_json():
    assert ai._looks_like_json('{"a": 1}')
    assert not ai._looks_like_json("<html>")


# ------------------------------------------------------------ normalization


def test_normalize_fact_free_with_no_price_becomes_zero():
    c = ai._normalize_fact({"model_name": "FreeModel", "free": True, "confidence": 0.9},
                           "test", "https://x", "2026-01-01T00:00:00+00:00")
    assert c["deployment"].price_in == 0.0 and c["deployment"].price_out == 0.0
    assert c["deployment"].zero_price is True
    assert c["weights"].weights_id == "slug:freemodel"


def test_normalize_fact_maps_fields():
    c = ai._normalize_fact(FACTS["models"][0], "test", "https://x",
                           "2026-01-01T00:00:00+00:00")
    assert c["deployment"].deploy_id == "acme:acme/qwen3-30b"
    assert c["deployment"].price_in == 0.05
    assert c["weights"].benchmark["aa_intelligence"] == 60.0
    assert c["deployment"].caps["tools"] is True
    assert c["deployment"].caps["structured"] is None  # three-valued, not False


def test_normalize_fact_skips_blank_name():
    assert ai._normalize_fact({"model_name": "  "}, "t", "https://x", "ts") is None


# ------------------------------------------------------------ validation


def test_validate_quarantines_price_disagreement(tmp_path):
    store = Store(tmp_path / "v.db")
    from mininfer.schema import Deployment, Weights
    store.upsert_weights(Weights("slug:qwen3-30b-a3b", "Qwen3-30B-A3B"))
    store.upsert_deployment(Deployment("acme:acme/qwen3-30b", "slug:qwen3-30b-a3b",
                                       "acme", "acme/qwen3-30b",
                                       price_in=0.05, price_out=0.15))
    store.commit()

    fact = dict(FACTS["models"][0])
    fact["price_in_usd_per_mtok"] = 0.50  # disagrees with registry 0.05
    c = ai._normalize_fact(fact, "add-url", "https://x", "ts")

    state = {"candidates": [c], "db": str(tmp_path / "v.db")}
    out = ai.validate_node(state)
    assert out["accepted"] == [] and len(out["rejected"]) == 1
    assert "disagrees" in out["rejected"][0]["reject_reason"]
    store.close()


# ------------------------------------------------------------ full graph


def _run(monkeypatch, tmp_path, facts, db=None):
    monkeypatch.setattr(ai, "_fetch_body", lambda url, source, ttl: HTML.encode())
    monkeypatch.setattr(ai, "_extract_facts",
                        lambda text, tables, st, url, agent: facts)
    db = db or str(tmp_path / "t.db")
    return ai.add_url("https://example.com/catalog", db=db, dry_run=False)


def test_full_graph_commits_accepted(tmp_path, monkeypatch):
    rep = _run(monkeypatch, tmp_path, FACTS)
    assert rep["models_found"] == 1 and rep["accepted"] == 1
    assert rep["accepted_deployments"] == ["acme:acme/qwen3-30b"]
    assert rep["error"] is None

    store = Store(tmp_path / "t.db")
    row = store.conn.execute(
        "SELECT price_in, price_out, context_window FROM deployments"
        " WHERE deploy_id='acme:acme/qwen3-30b'").fetchone()
    assert row is not None and row["price_in"] == 0.05 and row["context_window"] == 32000
    assert store.conn.execute(
        "SELECT COUNT(*) c FROM evidence WHERE entity_id='acme:acme/qwen3-30b'"
    ).fetchone()["c"] >= 1  # provenance is mandatory
    store.close()


@pytest.mark.skipif(bool(os.environ.get("MI_TEST_PG_DSN")),
                    reason="reads the file with sqlite3 directly")
def test_full_graph_quarantines_on_disagreement(tmp_path, monkeypatch):
    # Seed the registry with the "true" provider-API price first.
    from mininfer.schema import Deployment, Weights
    db = str(tmp_path / "q.db")
    store = Store(db)
    store.upsert_weights(Weights("slug:qwen3-30b-a3b", "Qwen3-30B-A3B"))
    store.upsert_deployment(Deployment("acme:acme/qwen3-30b", "slug:qwen3-30b-a3b",
                                       "acme", "acme/qwen3-30b",
                                       price_in=0.05, price_out=0.15))
    store.commit()
    store.close()

    facts = {"source_type": "pricing", "models": [dict(FACTS["models"][0])]}
    facts["models"][0]["price_in_usd_per_mtok"] = 0.50  # scraped, wrong
    rep = _run(monkeypatch, tmp_path, facts, db=db)

    assert rep["accepted"] == 0 and rep["quarantined"] == 1
    # The scraped price must NOT have overwritten the provider-API price.
    c = sqlite3.connect(db)
    assert c.execute(
        "SELECT price_in FROM deployments WHERE deploy_id='acme:acme/qwen3-30b'"
    ).fetchone()[0] == 0.05
    assert c.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] >= 1
    c.close()


def test_full_graph_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "_fetch_body", lambda url, source, ttl: HTML.encode())
    monkeypatch.setattr(ai, "_extract_facts",
                        lambda text, tables, st, url, agent: FACTS)
    rep = ai.add_url("https://example.com/catalog", db=str(tmp_path / "t.db"),
                     dry_run=True)
    assert rep["accepted"] == 1 and rep["dry_run"] is True
    store = Store(tmp_path / "t.db")
    assert store.counts()["deployments"] == 0
    store.close()
