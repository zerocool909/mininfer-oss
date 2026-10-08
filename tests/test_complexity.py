"""Tests for mininfer/complexity.py.

Validates:
1. Fast heuristic detection of math, logic, multi-step, and debugging signals.
2. Attenuation from negative signals (extraction, translation, formatting).
3. Ambiguity detection and confidence margin calculation.
4. LLM judge tie-breaking tier and error/timeout fallback.
5. Caching and overrides.
"""
from __future__ import annotations

import pytest

from mininfer.complexity import (
    Complexity,
    JudgeCache,
    build_judge_messages,
    classify_complexity,
    estimate_heuristic,
    extract_signals,
    parse_judge_output,
    score_signals,
)


def test_simple_extraction_is_low_complexity():
    prompt = "Extract all email addresses and phone numbers from this text: foo@bar.com"
    comp = estimate_heuristic(prompt)
    assert comp.level == "low"
    assert not comp.needs_reasoning
    assert comp.confidence >= 0.75
    assert comp.source == "heuristic"


def test_translation_and_formatting_are_low():
    prompt = "Translate this paragraph into French and format as markdown bullets."
    comp = estimate_heuristic(prompt)
    assert comp.level == "low"
    assert not comp.needs_reasoning


def test_math_proof_is_high_complexity():
    prompt = "Prove by induction that sum(i^2, i=1..n) = n(n+1)(2n+1)/6 for all n >= 1."
    comp = estimate_heuristic(prompt)
    assert comp.level == "high"
    assert comp.needs_reasoning
    assert comp.confidence >= 0.75
    assert any("math_logic" in s for s in comp.signals)


def test_paths_urls_and_dates_are_not_math():
    """Regression: `math_logic` used to match any string containing two slashes.

    The loose arithmetic alternative treated `/` and `-` as operators, so a file
    path or a URL scored 0.85 -> HIGH and the *most trivial* agentic steps ("read
    this file", "fetch this URL") were routed to the reasoning tier. A bare ISO
    date matched as well. Arithmetic has to be digit-anchored on both sides.
    """
    for prompt in (
        "Read mininfer/proxy.py and deploy/cloud/k8s.yaml, then list what they do",
        "Fetch https://api.github.com/repos/zerocool909/mininfer-oss and count the files",
        "Summarize the changes in config/policy.yaml and docs/api-contract.md",
        "Summarize the incidents reported on 2026-10-07 and what changed",
    ):
        comp = estimate_heuristic(prompt)
        assert comp.level == "low", f"{prompt!r} -> {comp.level} {comp.signals}"
        assert not comp.needs_reasoning, prompt
        assert not any("math_logic" in s for s in comp.signals), comp.signals


def test_real_arithmetic_is_still_math():
    """Tightening the pattern must not stop it detecting actual mathematics."""
    for prompt in ("What is 12 * 7?", "Solve 5 - 3 = 2x for x", "compute 2^10"):
        comp = estimate_heuristic(prompt)
        assert any("math_logic" in s for s in comp.signals), f"{prompt!r} -> {comp.signals}"


def test_concurrency_debugging_is_high_complexity():
    prompt = """
    Traceback (most recent call last):
      File "server.py", line 42, in handle_conn
    We have a race condition in the mutex lock acquisition leading to a deadlock.
    Find the bug and fix the race condition.
    """
    comp = estimate_heuristic(prompt)
    assert comp.level == "high"
    assert comp.needs_reasoning
    assert any("code_debug" in s for s in comp.signals)


def test_multi_step_planning_increases_score():
    prompt = "Step by step, plan and execute a migration strategy with trade-offs and pros and cons."
    comp = estimate_heuristic(prompt)
    assert comp.score >= 0.35
    assert any("multi_step" in s for s in comp.signals)


def test_negative_signals_counterbalance_moderate_length():
    prompt = "Extract all dates and prices from this 800-character table. Summarize in 3 bullets." + (" lorem ipsum" * 20)
    score, active = score_signals(prompt)
    # The negative signal (extract + summarize) pulls the score down
    assert any("negative" in s for s in active)
    comp = estimate_heuristic(prompt)
    assert comp.level in ("low", "medium")
    assert not comp.needs_reasoning


def test_override_bypasses_heuristics():
    prompt = "Prove P != NP"
    comp = classify_complexity(prompt, override="low")
    assert comp.level == "low"
    assert not comp.needs_reasoning
    assert comp.source == "override"


def test_judge_not_called_when_heuristic_is_confident():
    consulted: list[str] = []

    def mock_judge(p: str):
        consulted.append(p)
        return {"level": "high", "needs_reasoning": True}

    clear_simple = "What is the capital of France?"
    comp = classify_complexity(clear_simple, judge=mock_judge, heuristic_confident=0.75)
    assert comp.source == "heuristic"
    assert not consulted


def test_judge_called_when_ambiguous():
    consulted: list[str] = []

    def mock_judge(p: str):
        consulted.append(p)
        return {"level": "high", "needs_reasoning": True, "reason": "subtle trade-off question"}

    # Moderate ambiguous prompt with mild signals in the medium band
    ambiguous_prompt = "Compare these two database indexing strategies for a hybrid search workload with mixed constraints."
    comp = classify_complexity(ambiguous_prompt, judge=mock_judge, heuristic_confident=0.75)

    assert consulted, "Judge should have been consulted for ambiguous prompt"
    assert comp.source == "judge"
    assert comp.level == "high"
    assert comp.needs_reasoning
    assert comp.reason == "subtle trade-off question"


def test_judge_fallback_on_exception():
    def failing_judge(p: str):
        raise TimeoutError("LLM call timed out")

    ambiguous_prompt = "Compare these two database indexing strategies for a hybrid workload."
    comp = classify_complexity(ambiguous_prompt, judge=failing_judge)
    assert comp.source == "heuristic"  # Safely fell back


def test_judge_fallback_on_garbage_string():
    def garbage_judge(p: str):
        return "I am unable to answer this request."

    ambiguous_prompt = "Compare these two database indexing strategies for a hybrid workload."
    comp = classify_complexity(ambiguous_prompt, judge=garbage_judge)
    assert comp.source == "heuristic"


def test_judge_cache_avoids_repeated_calls():
    calls: list[int] = []

    def counting_judge(p: str):
        calls.append(1)
        return '{"level": "high", "needs_reasoning": true, "reason": "complex logic"}'

    cache = JudgeCache()
    prompt = "Compare indexing strategy A versus B for our database workload."

    r1 = classify_complexity(prompt, judge=counting_judge, cache=cache)
    r2 = classify_complexity(prompt, judge=counting_judge, cache=cache)

    assert len(calls) == 1
    assert r1.level == "high"
    assert r2.level == "high"


def test_parse_judge_output_json_and_markdown():
    valid_json = '{"level": "high", "needs_reasoning": true, "reason": "algebraic calculation"}'
    parsed = parse_judge_output(valid_json)
    assert parsed is not None
    assert parsed["level"] == "high"
    assert parsed["needs_reasoning"] is True

    fenced_json = '```json\n{"level": "low", "needs_reasoning": false, "reason": "simple lookup"}\n```'
    parsed_fenced = parse_judge_output(fenced_json)
    assert parsed_fenced is not None
    assert parsed_fenced["level"] == "low"
    assert parsed_fenced["needs_reasoning"] is False

    invalid = "not json at all"
    assert parse_judge_output(invalid) is None


def test_build_judge_messages():
    msgs = build_judge_messages("Solve 2x + 5 = 15")
    assert len(msgs) >= 3
    assert msgs[0]["role"] == "system"
    assert msgs[-1]["role"] == "user"
    assert "Solve 2x + 5 = 15" in msgs[-1]["content"]


def test_as_dict():
    comp = Complexity(
        level="high",
        needs_reasoning=True,
        confidence=0.85,
        score=0.72,
        signals=("math_logic:0.8", "multi_step:0.5"),
        source="heuristic",
        reason="test reason",
    )
    d = comp.as_dict()
    assert d["level"] == "high"
    assert d["needs_reasoning"] is True
    assert d["confidence"] == 0.85
    assert len(d["signals"]) == 2


def test_adapt_task_for_complexity():
    from mininfer.complexity import adapt_task_for_complexity
    from mininfer.schema import TaskProfile

    base = TaskProfile(
        name="general_chat",
        tokens_in=3000,
        tokens_out=1000,
        require={"tools": False},
        min_context=4000,
        min_success_lb=0.6,  # already a demanding floor: an absolute reasoning_floor did nothing here
    )

    comp_high = Complexity(
        level="high",
        needs_reasoning=True,
        confidence=0.9,
        score=0.8,
        source="heuristic",
    )
    adapted_high = adapt_task_for_complexity(base, comp_high, floor_delta=0.08)
    # Difficulty moves the bar, never the capability set: a heuristic guess is not
    # a capability fact, and `require reasoning` emptied pools on reject-tasks.
    assert adapted_high.require == {"tools": False}
    assert adapted_high.min_success_lb == pytest.approx(0.68)
    assert adapted_high.name == "general_chat"

    # Low complexity tightening budget
    comp_low = Complexity(
        level="low",
        needs_reasoning=False,
        confidence=1.0,
        score=0.0,
        source="heuristic",
    )
    adapted_low = adapt_task_for_complexity(base, comp_low)
    assert adapted_low.tokens_in == 1500
    assert adapted_low.tokens_out == 500
    assert adapted_low.require == {"tools": False}
    assert adapted_low.min_success_lb == 0.6
    assert adapted_low.name == "general_chat"


def test_cli_complexity_command(capsys):
    from mininfer import cli

    rc = cli.main(["complexity", "Prove by induction that sum(i^2) = n(n+1)(2n+1)/6"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "COMPLEXITY   HIGH" in captured.out
    assert "needs_reasoning=True" in captured.out
    assert "math_logic" in captured.out


def test_cli_complexity_json(capsys):
    import json
    from mininfer import cli

    rc = cli.main(["complexity", "Extract all emails", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["level"] == "low"
    assert data["needs_reasoning"] is False
    assert "confidence" in data


def test_cli_complexity_report(capsys, tmp_path):
    import json
    from mininfer import cli
    from mininfer.store import Store

    db = tmp_path / "test_report.db"
    store = Store(db)
    reason = json.dumps({
        "complexity": {
            "level": "high",
            "needs_reasoning": True,
            "confidence": 0.9,
            "score": 0.8,
            "signals": ["math_logic: induction"],
            "source": "heuristic",
        },
        "complexity_escalated": True,
    })
    store.record_decision(
        task="general_chat",
        policy="default",
        mode="auto",
        chosen="openrouter:r1",
        candidates=["openrouter:r1"],
        reason=reason,
    )
    store.commit()
    store.close()

    rc = cli.main(["--db", str(db), "complexity", "--report"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "COMPLEXITY DECISIONS SUMMARY" in captured.out
    assert "HIGH" in captured.out
    assert "ESCALATIONS" in captured.out


def test_cli_route_with_complexity(capsys, tmp_path, monkeypatch):
    import json
    from mininfer import cli
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    db = tmp_path / "test_route.db"
    store = Store(db)
    store.upsert_weights(Weights("hf:test/r1", "r1", params_b=70.0, benchmark={"aa_intelligence": 99.0}))
    store.upsert_deployment(Deployment("openrouter:r1", "hf:test/r1", "openrouter", "r1",
                                       price_in=1.0, price_out=1.0, context_window=32000,
                                       caps={"reasoning": True}))
    # also add a non-reasoning deployment
    store.upsert_weights(Weights("hf:test/small", "small", params_b=7.0, benchmark={"aa_intelligence": 60.0}))
    store.upsert_deployment(Deployment("openrouter:small", "hf:test/small", "openrouter", "small",
                                       price_in=0.1, price_out=0.1, context_window=32000,
                                       caps={"reasoning": False}))
    store.commit()
    store.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"openrouter"})

    rc = cli.main([
        "--db", str(db),
        "route", "general_chat",
        "--prompt", "Prove that sqrt(2) is irrational using contradiction.",
        "--include-uncredentialed",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "COMPLEXITY HIGH" in captured.out
    assert "needs_reasoning=True" in captured.out
    assert "math_logic" in captured.out
    assert "openrouter:r1" in captured.out


def test_cli_explain_with_complexity(capsys, tmp_path, monkeypatch):
    from mininfer import cli
    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    db = tmp_path / "test_explain.db"
    store = Store(db)
    store.upsert_weights(Weights("hf:test/r1", "r1", params_b=70.0, benchmark={"aa_intelligence": 99.0}))
    store.upsert_deployment(Deployment("openrouter:r1", "hf:test/r1", "openrouter", "r1",
                                       price_in=1.0, price_out=1.0, context_window=32000,
                                       caps={"reasoning": True}))
    store.commit()
    store.close()

    monkeypatch.setattr("mininfer.router.available_providers", lambda **k: {"openrouter"})

    rc = cli.main([
        "--db", str(db),
        "explain", "general_chat",
        "--complexity", "high",
        "--include-uncredentialed",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "COMPLEXITY: HIGH" in captured.out


# --------------------------------------------------------------------------- #
# Tier 2 — the injected judge (opt-in)
# --------------------------------------------------------------------------- #

def test_make_judge_is_off_unless_explicitly_enabled():
    """A missing or disabled block must leave the zero-latency heuristic path."""
    from mininfer.complexity import make_judge

    assert make_judge(None) is None
    assert make_judge({}) is None
    assert make_judge({"enabled": False, "model": "groq:small"}) is None
    assert make_judge({"enabled": True}) is None, "no pinned model -> not a judge"
    assert make_judge({"enabled": True, "model": "groq:small"}) is not None


def test_make_judge_pins_the_model_and_never_raises():
    """Pinned, so it cannot recurse through the router; failures are swallowed."""
    from mininfer.complexity import make_judge

    seen: dict = {}

    class _Res:
        ok = True
        text = '{"level": "high", "needs_reasoning": true, "reason": "pinned"}'

    def factory():
        def runner(deploy_id, messages, **kw):
            seen["deploy_id"] = deploy_id
            seen["messages"] = messages
            return _Res()
        return runner

    out = make_judge({"enabled": True, "model": "groq:small"}, runner_factory=factory)("Prove it")
    assert seen["deploy_id"] == "groq:small"
    assert seen["messages"][0]["role"] == "system"
    assert "high" in out

    def boom():
        def runner(*a, **k):
            raise RuntimeError("upstream down")
        return runner

    assert make_judge({"enabled": True, "model": "groq:small"}, runner_factory=boom)("q") is None


def test_the_judge_is_consulted_only_when_the_heuristic_is_unsure():
    """The gate that keeps the judge off the ~most of the request path."""
    from mininfer.complexity import classify_complexity

    calls: list[str] = []

    def judge(prompt):
        calls.append(prompt)
        return {"level": "high", "needs_reasoning": True, "reason": "judged"}

    confident = classify_complexity("Hello, how are you?", judge=judge, cache=JudgeCache())
    assert confident.source == "heuristic"
    assert not calls, "a confident heuristic must not spend a judge call"

    # A medium-band prompt (confidence 0.53 < 0.75) is the ambiguous middle.
    # A private cache keeps this hermetic: the default one is a module global that
    # another test may already have filled for the same prompt.
    ambiguous = classify_complexity("First refactor the module, then update the tests",
                                    judge=judge, cache=JudgeCache())
    assert calls, "an ambiguous prompt should consult the judge"
    assert ambiguous.source == "judge"
    assert ambiguous.level == "high"


# --------------------------------------------------------------------------- #
# Calibration
# --------------------------------------------------------------------------- #

def test_the_shipped_thresholds_are_joint_best_on_the_labelled_set():
    """The cut points are supposed to be chosen from data, not from taste.

    If a weight or pattern changes and the shipped 0.25/0.65 stops being
    joint-best, this fails — re-run `mi complexity --calibrate` and update the
    policy deliberately rather than letting the defaults drift.
    """
    from mininfer.complexity import CALIBRATION_SET, calibrate, sweep_thresholds

    assert len(CALIBRATION_SET) >= 20
    current = calibrate()                     # defaults are the shipped values
    assert current["accuracy"] >= 0.85, current

    ranked = sweep_thresholds()
    best = ranked[0]["accuracy"]
    assert current["accuracy"] == best, (current["accuracy"], best)
    joint_best = {(r["low_max"], r["high_min"]) for r in ranked if r["accuracy"] == best}
    assert (0.25, 0.65) in joint_best, sorted(joint_best)


def test_cli_complexity_calibrate(capsys):
    from mininfer import cli

    assert cli.main(["complexity", "--calibrate"]) == 0
    out = capsys.readouterr().out
    assert "CALIBRATION SET" in out
    assert "CURRENT THRESHOLDS" in out
    assert "floor_delta" in out, "the report must say what it does NOT calibrate"


