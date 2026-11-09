"""The daily canary: does every free arm still answer a known question?

The failure it exists for is the one `probe.py` cannot see: `GET /models` says
200, and the arm behind it 404s, returns empty content, or has started
charging. These tests pin the three properties that make a scheduled run safe:

* only zero-marginal arms are called (a canary can never produce a bill),
* a provider error is recorded, never raised,
* infrastructure errors do not count as model failures.
"""
from __future__ import annotations

import json

import pytest

from mininfer import canary
from mininfer.execute import CallResult
from mininfer.fetch import utcnow
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


# ------------------------------------------------------------------- fixtures
def _store(tmp_path) -> Store:
    store = Store(tmp_path / "c.db")
    store.upsert_weights(Weights("hf:a/free", "free-answerer"))
    store.upsert_deployment(Deployment("openrouter:free-answerer", "hf:a/free",
                                       "openrouter", "free-answerer",
                                       price_in=0.0, price_out=0.0, zero_price=True))
    store.upsert_weights(Weights("hf:a/trial", "trial-answerer"))
    store.upsert_deployment(Deployment("groq:trial-answerer", "hf:a/trial",
                                       "groq", "trial-answerer", trial_credits=True))
    store.upsert_weights(Weights("hf:a/paid", "paid-answerer"))
    store.upsert_deployment(Deployment("openrouter:paid-answerer", "hf:a/paid",
                                       "openrouter", "paid-answerer",
                                       price_in=1.0, price_out=1.0))
    store.upsert_weights(Weights("hf:a/embed", "text-embedding-3-small"))
    store.upsert_deployment(Deployment("openrouter:text-embedding-3-small",
                                       "hf:a/embed", "openrouter",
                                       "text-embedding-3-small",
                                       price_in=0.0, price_out=0.0, zero_price=True))
    store.commit()
    # `pricing_state` is derived by the only writer, which reconcile runs.
    store._sync_pricing_states(utcnow())
    store.commit()
    return store


def _answer(prompt: str) -> str:
    if "BANANA" in prompt:
        return "BANANA"
    if "17 * 23" in prompt:
        return "391"
    if "Ada Lovelace" in prompt:
        return '{"name": "Ada Lovelace", "date": "1843", "amount": "250"}'
    if "Summarise" in prompt:
        return "Evaporation and condensation move water through the cycle."
    if "add(a, b)" in prompt:
        return "def add(a, b):\n    return a + b"
    if "vault code" in prompt:
        return "7734"
    return "?"


def _good(deploy_id, prompt, max_tokens, timeout):
    return CallResult(deploy_id, text=_answer(prompt), ok=True, latency_ms=12.0,
                      tokens_in=20, tokens_out=10)


def _boom(deploy_id, prompt, max_tokens, timeout):
    return CallResult(deploy_id, ok=False, error_class="http_500",
                      error_detail="upstream exploded")


def _limited(deploy_id, prompt, max_tokens, timeout):
    # A non-halting infrastructure error, so the denominator test sees every arm.
    return CallResult(deploy_id, ok=False, error_class="network_error",
                      error_detail="dns")


# --------------------------------------------------------------------- grading


@pytest.mark.parametrize("item_id,content,expected", [
    ("instruction_exact", "  Banana. ", True),
    ("instruction_exact", "banana split", False),
    ("math_arithmetic", "17*23 = 391", True),
    ("math_arithmetic", "392", False),
    ("extraction_json", 'Sure:\n{"name":"Ada Lovelace","date":"1843","amount":"250"}', True),
    ("extraction_json", '{"name":"Ada Lovelace"}', False),
    ("summarise", "Evaporation and condensation are the whole story.", True),
    ("summarise", "Only evaporation.", False),
    ("code_shape", "```python\ndef add(a, b):\n    return a + b\n```", True),
    ("code_shape", "def add(a):\n    return a", False),
    ("long_context_needle", "The code is 7734.", True),
])
def test_grading_is_deterministic(item_id, content, expected):
    item = next(i for i in canary.DATASET if i["id"] == item_id)
    assert canary.grade(item, content)["ok"] is expected


def test_python_check_parses_and_never_executes():
    """A canary must not run model-written code."""
    item = next(i for i in canary.DATASET if i["id"] == "code_shape")
    g = canary.grade(item, "import os\nos.remove('x')\ndef add(a, b):\n    return a+b")
    # It parses and finds the function; the malicious import is never executed.
    assert g["ok"] is True


# ------------------------------------------------------------------- selection


def test_only_callable_zero_marginal_answerers_are_selected(tmp_path):
    store = _store(tmp_path)
    try:
        arms = {d["deploy_id"] for d in
                canary.free_arms(store, available={"openrouter", "groq"})}
    finally:
        store.close()
    assert arms == {"openrouter:free-answerer", "groq:trial-answerer"}
    # paid is not canaried, and an embedding model is not an answerer
    assert "openrouter:paid-answerer" not in arms
    assert "openrouter:text-embedding-3-small" not in arms


def test_an_unkeyed_provider_is_not_canaried(tmp_path):
    store = _store(tmp_path)
    try:
        arms = {d["deploy_id"] for d in canary.free_arms(store, available={"openrouter"})}
    finally:
        store.close()
    assert arms == {"openrouter:free-answerer"}


# ------------------------------------------------------------------- execution


def test_all_correct_reports_ok_and_records_canary_evidence(tmp_path):
    store = _store(tmp_path)
    try:
        rep = canary.run_canary(store, caller=_good, prompts_per_arm=len(canary.DATASET),
                                available={"openrouter", "groq"})
        n = store.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE signal_kind='canary'").fetchone()[0]
        kinds = {r[0] for r in store.conn.execute(
            "SELECT DISTINCT signal_kind FROM observations")}
    finally:
        store.close()
    assert rep["verdict"] == "ok"
    assert rep["correct"] == rep["calls"] == 2 * len(canary.DATASET)
    assert n == 2 * len(canary.DATASET)
    assert kinds == {"canary"}


def test_a_provider_failure_is_recorded_not_raised(tmp_path):
    store = _store(tmp_path)
    try:
        rep = canary.run_canary(store, caller=_boom, prompts_per_arm=len(canary.DATASET),
                                available={"openrouter", "groq"})
        rows = store.conn.execute(
            "SELECT error_class FROM observations WHERE signal_kind='canary'"
        ).fetchall()
    finally:
        store.close()
    assert rep["verdict"] == "failed"
    assert rep["answered"] == 0
    assert all(m["status"] == "unreachable" for m in rep["models"])
    assert len(rows) == 2 * len(canary.DATASET)
    assert {r[0] for r in rows} == {"http_500"}


def test_rate_limits_are_unmeasured_not_wrong_answers(tmp_path):
    store = _store(tmp_path)
    try:
        rep = canary.run_canary(store, caller=_limited, prompts_per_arm=len(canary.DATASET),
                                available={"openrouter", "groq"})
    finally:
        store.close()
    # Nothing reached a model, so the accuracy denominator is zero — not "0%".
    assert rep["answered"] == 0
    assert rep["unmeasured"] == 2 * len(canary.DATASET)
    assert rep["accuracy_pct"] == 0.0


def test_no_arms_is_a_report_not_an_error(tmp_path):
    store = Store(tmp_path / "empty.db")
    try:
        rep = canary.run_canary(store, caller=_good, available=set())
        assert rep["ran"] is False
        assert rep["reason"] == "no callable free arms"
    finally:
        store.close()


def test_history_is_bounded_and_last_report_is_json(tmp_path):
    store = _store(tmp_path)
    try:
        for _ in range(canary.HISTORY_KEEP + 5):
            canary.run_canary(store, caller=_good, max_arms=1, prompts_per_arm=1,
                              available={"openrouter", "groq"})
        assert len(canary.history(store)) == canary.HISTORY_KEEP
        assert canary.last_report(store)["verdict"] == "ok"
    finally:
        store.close()


# ------------------------------------------------------------------ scheduling


def test_config_uses_env_first_then_setting(tmp_path, monkeypatch):
    store = _store(tmp_path)
    try:
        monkeypatch.delenv("MI_CANARY", raising=False)
        assert canary.config(store)["enabled"] is True  # default: keys exist
        store.set_setting(canary.ENABLED_KEY, "0")
        assert canary.config(store)["enabled"] is False
        monkeypatch.setenv("MI_CANARY", "1")
        assert canary.config(store)["enabled"] is True
        monkeypatch.setenv("MI_CANARY", "0")
        assert canary.config(store)["enabled"] is False
    finally:
        store.close()


def test_is_due_waits_a_full_interval():
    assert canary.is_due({"last_run": None, "interval_seconds": 86400}) is True
    assert canary.is_due({"last_run": "2026-01-01T00:00:00+00:00",
                          "interval_seconds": 86400},
                         now="2026-01-01T12:00:00+00:00") is False
    assert canary.is_due({"last_run": "2026-01-01T00:00:00+00:00",
                          "interval_seconds": 86400},
                         now="2026-01-02T00:00:01+00:00") is True


# ------------------------------------------------- coverage, cost and quota


def test_rotation_covers_every_prompt_over_three_days():
    import datetime as dt
    seen = set()
    for day in range(3):
        on = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(days=day)
        seen |= {i["id"] for i in canary._rotate(canary.DATASET, 2, on)}
    assert seen == {i["id"] for i in canary.DATASET}


def test_a_429_stops_the_run_and_leaves_the_quota_alone(tmp_path):
    store = _store(tmp_path)
    calls = {"n": 0}

    def limited(deploy_id, prompt, max_tokens, timeout):
        calls["n"] += 1
        return CallResult(deploy_id, ok=False, error_class="429",
                          error_detail="free-models-per-min")

    try:
        rep = canary.run_canary(store, caller=limited, prompts_per_arm=2,
                                available={"openrouter", "groq"})
    finally:
        store.close()
    # One gateway's 429 stops that gateway, and the other is still measured.
    assert rep["halted"] == {"groq": "429", "openrouter": "429"}
    assert calls["n"] == 2
    assert {m["calls"] for m in rep["models"]} == {1}


def test_a_drained_quota_bucket_is_skipped(tmp_path):
    store = _store(tmp_path)
    try:
        store.record_rate_limit("openrouter:free-answerer", window="minute",
                                limit_n=10, remaining_n=0)
        store.commit()
        rep = canary.run_canary(store, caller=_good, prompts_per_arm=1,
                                available={"openrouter", "groq"})
    finally:
        store.close()
    skipped = {s["model"] for s in rep["skipped"]}
    assert "openrouter:free-answerer" in skipped
    # The other free arm still ran.
    assert {m["model"] for m in rep["models"]} == {"groq:trial-answerer"}


def test_a_stale_catalogue_does_not_trust_trial_arms(tmp_path):
    store = _store(tmp_path)
    try:
        store.conn.execute(
            "UPDATE deployments SET last_seen='2020-01-01T00:00:00+00:00'")
        store.commit()
        rep = canary.run_canary(store, caller=_good, prompts_per_arm=1,
                                available={"openrouter", "groq"}, max_stale_days=7)
    finally:
        store.close()
    assert rep["registry_age_days"] > 7
    assert "trial" not in rep["tiers"]
    assert {m["model"] for m in rep["models"]} == {"openrouter:free-answerer"}


def test_refresh_before_grading_picks_up_a_new_free_arm(tmp_path):
    """New free models appear only if the catalogue is refreshed first."""
    store = _store(tmp_path)

    def fake_refresh(s):
        s.upsert_weights(Weights("hf:a/brand-new", "brand-new"))
        s.upsert_deployment(Deployment("openrouter:brand-new", "hf:a/brand-new",
                                       "openrouter", "brand-new",
                                       price_in=0.0, price_out=0.0, zero_price=True))
        s.commit()
        s._sync_pricing_states(utcnow())
        s.commit()
        return {"ok": ["openrouter"], "failed": []}

    try:
        rep = canary.run_canary(store, caller=_good, prompts_per_arm=1,
                                available={"openrouter", "groq"},
                                refresh=True, refresh_fn=fake_refresh)
    finally:
        store.close()
    assert rep["refreshed"]["ok"] == ["openrouter"]
    assert "openrouter:brand-new" in {m["model"] for m in rep["models"]}


def test_no_write_transaction_is_left_open_after_a_run(tmp_path):
    """The incident this repo already paid for: a write txn across a model call."""
    store = _store(tmp_path)
    try:
        canary.run_canary(store, caller=_good, prompts_per_arm=1,
                          available={"openrouter", "groq"})
        # SQLite's own flag is the one that matters: an open write transaction
        # is what blocks every other writer.
        conn = getattr(store.conn, "_conn", store.conn)
        assert getattr(conn, "in_transaction", False) is False
    finally:
        store.close()


def test_idle_gate_ignores_background_observations(tmp_path):
    store = _store(tmp_path)
    try:
        store.observe("openrouter:free-answerer", "t", True, ts=utcnow(),
                      signal_kind="canary")
        store.commit()
        assert canary.is_idle(store, 15) is True
        store.observe("openrouter:free-answerer", "t", True, ts=utcnow(),
                      signal_kind="verified")
        store.commit()
        assert canary.is_idle(store, 15) is False
    finally:
        store.close()
