"""Tests for RouterBench (verifiable rewards) and the model-calling layer.

The two things that must never silently drift are (1) a verifier accepting a
wrong answer, and (2) the HTTP error classification that turns failures into
routing signals. Both corrupt observations downstream with no error, so they
are pinned explicitly.
"""
from __future__ import annotations

import httpx
import pytest

from mininfer.bench import (
    TASKS,
    TASKS_BY_FAMILY,
    _rows_equal,
    build_tasks,
    gold_answer,
    run_bench,
    self_test,
    verify,
)
from mininfer.execute import CallResult, Endpoint, call, resolve_endpoint
from mininfer.store import Store


# --------------------------------------------------------------- self-check


def test_every_gold_answer_verifies():
    """The benchmark is only trustworthy if its own reference answers pass."""
    assert len(TASKS) == 42
    assert set(TASKS_BY_FAMILY) == {"sql", "extraction", "tool_call", "reasoning"}
    assert TASKS_BY_FAMILY["sql"]  # not empty, etc.
    passed, failed = self_test()
    assert failed == 0, f"{failed} gold answers failed to verify"
    assert passed == len(TASKS)


# ------------------------------------------------------------------- SQL


def test_sql_verifier_accepts_correct_answer_in_fence():
    t = TASKS_BY_FAMILY["sql"][0]
    ok, score = verify(t, gold_answer(t))
    assert ok and score == 1.0


def test_sql_verifier_rejects_wrong_rows():
    t = TASKS_BY_FAMILY["sql"][0]
    ok, _ = verify(t, "```sql\nSELECT name FROM products ORDER BY name\n```")
    assert not ok


def test_sql_verifier_tolerates_result_order():
    # `_rows_equal` compares as sets, so a different but valid ORDER BY still
    # passes — ordering is presentation, not the answer.
    assert _rows_equal([(1, "a"), (2, "b")], [(2, "b"), (1, "a")])
    assert not _rows_equal([(1, "a")], [(1, "a"), (2, "b")])


def test_sql_verifier_rejects_syntax_error():
    t = TASKS_BY_FAMILY["sql"][0]
    ok, _ = verify(t, "```sql\nSELEC name FROM products\n```")
    assert not ok


# -------------------------------------------------------------- extraction


def test_extraction_accepts_correct_json():
    t = TASKS_BY_FAMILY["extraction"][0]
    ok, _ = verify(t, gold_answer(t))
    assert ok


def test_extraction_rejects_missing_field():
    t = TASKS_BY_FAMILY["extraction"][0]
    ok, _ = verify(t, '{"customer": "John Smith"}')
    assert not ok


def test_extraction_rejects_wrong_value():
    t = TASKS_BY_FAMILY["extraction"][0]
    data = gold_answer(t).replace('"Pro Widget"', '"Basic Widget"')
    ok, _ = verify(t, data)
    assert not ok


# ------------------------------------------------------------------ tools


def test_tool_accepts_name_and_arguments():
    t = TASKS_BY_FAMILY["tool_call"][0]
    ok, _ = verify(t, gold_answer(t))
    assert ok


def test_tool_rejects_wrong_tool_name():
    t = TASKS_BY_FAMILY["tool_call"][0]
    ok, _ = verify(t, '{"name": "get_forecast", "arguments": {"city": "Lisbon"}}')
    assert not ok


def test_tool_accepts_bare_object_with_expected_args():
    t = TASKS_BY_FAMILY["tool_call"][0]
    ok, _ = verify(t, '{"city": "Lisbon"}')
    assert ok


def test_tool_accepts_arguments_as_json_string():
    t = TASKS_BY_FAMILY["tool_call"][0]
    ok, _ = verify(t, '{"name": "get_weather", "arguments": "{\\"city\\": \\"Lisbon\\"}"}')
    assert ok


# --------------------------------------------------------------- reasoning


def test_reasoning_extracts_trailing_number():
    t = TASKS_BY_FAMILY["reasoning"][0]
    ok, _ = verify(t, "The average speed is 80 km/h.")
    assert ok


def test_reasoning_rejects_wrong_number():
    t = TASKS_BY_FAMILY["reasoning"][0]
    ok, _ = verify(t, "The average speed is 90 km/h.")
    assert not ok


# ----------------------------------------------------------------- harness


def _perfect_runner():
    gold = {t.prompt: gold_answer(t) for t in TASKS}

    def run(deploy_id, messages, **kw):
        text = gold.get(messages[0]["content"], "")
        return CallResult(deploy_id, text=text, ok=True, tokens_in=100, tokens_out=50)

    return run


def test_run_bench_writes_verified_observations(tmp_path):
    store = Store(tmp_path / "b.db")
    rep = run_bench(store, "sql", "test:model", _perfect_runner(), write=True)
    assert rep.ok == 20 and rep.failed == 0 and rep.errors == 0

    stats = store.stats("sql_generation")["test:model"]
    assert stats["n"] == 20 and stats["wins"] == 20
    assert stats["mean_latency_ms"] is None  # no latency supplied
    store.close()


def test_run_bench_records_failures_as_bad_output(tmp_path):
    store = Store(tmp_path / "b.db")
    runner = lambda deploy_id, messages, **kw: CallResult(  # noqa: E731
        deploy_id, text="not the answer", ok=True)
    rep = run_bench(store, "reasoning", "test:model", runner, write=True)
    assert rep.ok == 0 and rep.failed == 6

    stats = store.stats("hard_reasoning")["test:model"]
    assert stats["n"] == 6 and stats["wins"] == 0
    store.close()


def test_run_bench_dry_run_does_not_write(tmp_path):
    store = Store(tmp_path / "b.db")
    run_bench(store, "sql", "test:model", _perfect_runner(), write=False)
    assert store.counts()["observations"] == 0
    store.close()


# ------------------------------------------------------------ endpoint layer


def test_resolve_openrouter_deploy():
    ep = resolve_endpoint("openrouter:qwen/qwen3.8-27b:free")
    assert ep.base_url == "https://openrouter.ai/api/v1"
    assert ep.model == "qwen/qwen3.8-27b:free"
    assert ep.error is None


def test_resolve_gateway_embedded_uses_gateway_host():
    ep = resolve_endpoint("hf/deepinfra:deepseek-ai/DeepSeek-V4-Flash")
    assert ep.base_url == "https://router.huggingface.co/v1"
    assert ep.model == "deepseek-ai/DeepSeek-V4-Flash"


def test_resolve_unknown_provider_refuses():
    ep = resolve_endpoint("mystery:x")
    assert ep.error is not None


def test_call_without_key_is_no_api_key():
    ep = Endpoint("https://example.com/v1", "m", None, {})
    res = call(ep, [], deploy_id="d")
    assert res.ok is False and res.error_class == "no_api_key"


def test_call_classifies_429(monkeypatch):
    def handler(request):
        return httpx.Response(429, json={"error": "rate limited"})

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler)))
    ep = Endpoint("https://example.com/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call(ep, [{"role": "user", "content": "hi"}], deploy_id="d")
    assert res.error_class == "429"


def test_call_parses_success_and_usage(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "42"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })

    real = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler)))
    ep = Endpoint("https://example.com/v1", "m", "k", {"Authorization": "Bearer k"})
    res = call(ep, [{"role": "user", "content": "hi"}], deploy_id="d")
    assert res.ok and res.text == "42"
    assert res.tokens_in == 10 and res.tokens_out == 2


def test_build_tasks_ids_are_unique():
    ids = [t.task_id for t in build_tasks()]
    assert len(ids) == len(set(ids))
