"""Batched LLM-as-judge.

Judging is a model call, so judging *k* arms one at a time costs *k* calls and
*k* latencies, each re-reading the same prompt. These tests pin the two things
that make the batch worth having — **one call for the whole batch**, and a
verdict per candidate plus a named best — and the failure behaviour that matters
most: a broken judge must never be read as evidence about a model.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from mininfer import judge
from mininfer.execute import CallResult

_GOOD = "This is a perfectly reasonable answer to the question."


class _Runner:
    """Records every call; answers trials and judge calls differently."""

    def __init__(self, *, judge_text: str = "", judge_error: str | None = None,
                 trial_text: str = "an answer"):
        self.judge_text = judge_text
        self.judge_error = judge_error
        self.trial_text = trial_text
        self.trials: list[str] = []
        self.judges: list[list[dict]] = []

    def __call__(self, deploy_id, messages, **kw):
        if messages and messages[0].get("role") == "system":
            self.judges.append(messages)
            if self.judge_error:
                return CallResult(deploy_id, error_class=self.judge_error)
            return CallResult(deploy_id, text=self.judge_text, ok=True)
        self.trials.append(deploy_id)
        return CallResult(deploy_id, text=self.trial_text, ok=True)


# --------------------------------------------------------------------------- #
# heuristics run first and are free
# --------------------------------------------------------------------------- #


def test_heuristics_reject_empty_and_error_bodies_without_a_call():
    runner = _Runner(judge_text="BEST: A")
    verdicts, _ = judge.evaluate_batch(
        "a prompt that is definitely long enough",
        [("A", _GOOD), ("B", ""), ("C", '{"error": {"message": "boom"}}')],
        runner=runner, judge_model="m")

    assert verdicts["A"][0] is True
    assert verdicts["B"] == (False, "empty_response")
    assert verdicts["C"][0] is False and verdicts["C"][1].startswith("error_in_body")
    # The candidates that failed heuristics must not be sent to the judge.
    sent = runner.judges[0][1]["content"]
    assert "Candidate A" in sent
    assert "Candidate B" not in sent and "Candidate C" not in sent


# --------------------------------------------------------------------------- #
# one call for the whole batch
# --------------------------------------------------------------------------- #


def test_a_batch_of_three_costs_exactly_one_judge_call():
    runner = _Runner(judge_text=(
        "CANDIDATE A: PASS - best\n"
        "CANDIDATE B: FAIL - misses the point\n"
        "CANDIDATE C: PASS - fine\n"
        "BEST: A"))

    verdicts, best = judge.evaluate_batch(
        "q", [("A", _GOOD), ("B", _GOOD), ("C", _GOOD)],
        runner=runner, judge_model="m")

    assert len(runner.judges) == 1, "the whole batch must be one call"
    assert verdicts["A"][0] is True
    assert verdicts["B"] == (False, "judge_fail: misses the point")
    assert verdicts["C"][0] is True
    assert best == "A"


def test_the_batch_prompt_carries_the_reference_response():
    runner = _Runner(judge_text="CANDIDATE A: PASS - matches\nBEST: A")
    judge.evaluate_batch("q", [("A", _GOOD)],
                         reference_text="the primary model's answer",
                         runner=runner, judge_model="m")
    assert "the primary model's answer" in runner.judges[0][1]["content"]


# --------------------------------------------------------------------------- #
# a broken judge is not evidence
# --------------------------------------------------------------------------- #


def test_a_failing_judge_leaves_the_heuristic_verdict():
    runner = _Runner(judge_error="429")
    verdicts, best = judge.evaluate_batch("q", [("A", _GOOD)], runner=runner, judge_model="m")
    assert verdicts["A"] == (True, "heuristics_passed")
    assert best == "A"


def test_unparseable_judge_output_leaves_the_heuristic_verdict():
    runner = _Runner(judge_text="Honestly they all look fine to me.")
    verdicts, best = judge.evaluate_batch("q", [("A", _GOOD)], runner=runner, judge_model="m")
    assert verdicts["A"] == (True, "heuristics_passed")
    assert best == "A"


def test_no_runner_means_heuristics_only():
    verdicts, best = judge.evaluate_batch("q", [("A", _GOOD)], runner=None, judge_model=None)
    assert verdicts["A"][0] is True and best == "A"


# --------------------------------------------------------------------------- #
# picking the best
# --------------------------------------------------------------------------- #


def test_best_falls_back_to_the_first_pass_when_the_line_is_missing():
    runner = _Runner(judge_text="CANDIDATE A: FAIL - bad\nCANDIDATE B: PASS - good")
    _, best = judge.evaluate_batch("q", [("A", _GOOD), ("B", _GOOD)],
                                   runner=runner, judge_model="m")
    assert best == "B"


def test_best_naming_an_unknown_label_is_ignored():
    runner = _Runner(judge_text="CANDIDATE A: PASS - ok\nBEST: Z")
    _, best = judge.evaluate_batch("q", [("A", _GOOD)], runner=runner, judge_model="m")
    assert best == "A"


def test_no_best_when_nothing_passed():
    runner = _Runner(judge_text="CANDIDATE A: FAIL - bad\nBEST: A")
    verdicts, best = judge.evaluate_batch("q", [("A", _GOOD)], runner=runner, judge_model="m")
    assert verdicts["A"][0] is False
    assert best is None


# --------------------------------------------------------------------------- #
# the single-candidate path is a batch of one
# --------------------------------------------------------------------------- #


def test_evaluate_trial_response_is_a_batch_of_one():
    runner = _Runner(judge_text="CANDIDATE A: FAIL - nope\nBEST: A")
    ok, reason = judge.evaluate_trial_response("q", _GOOD, runner=runner, judge_model="m")
    assert ok is False and reason.startswith("judge_fail")
    assert len(runner.judges) == 1


# --------------------------------------------------------------------------- #
# the shadow trial wires the batch through
# --------------------------------------------------------------------------- #

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
  free_floor_exempt: true
  free_trial_obs: 3
  judge_batch_size: 3
tasks:
  t:
    description: test task
    tokens_in: 10
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
"""


def test_a_shadow_request_trials_the_batch_and_judges_it_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    (tmp_path / "policy.yaml").write_text(_POLICY)
    monkeypatch.setenv("MI_POLICY", str(tmp_path / "policy.yaml"))

    from mininfer.schema import Deployment, Weights
    from mininfer.store import Store

    s = Store(tmp_path / "p.db")
    s.upsert_weights(Weights("hf:m", "m", params_b=7.0))
    # Local provider, so no key is needed to be "callable".
    s.upsert_deployment(Deployment("ollama:primary", "hf:m", "ollama", "primary",
                                   price_in=0.0, price_out=0.0, zero_price=True,
                                   context_window=1000))
    for i in range(3):
        s.upsert_deployment(Deployment(f"ollama:t{i}", "hf:m", "ollama", f"t{i}",
                                       price_in=0.0, price_out=0.0, zero_price=True,
                                       context_window=1000))
    s.commit()
    s.close()

    import mininfer.proxy as proxy
    runner = _Runner(judge_text=(
        "CANDIDATE A: PASS - best of the three\n"
        "CANDIDATE B: PASS - acceptable\n"
        "CANDIDATE C: FAIL - wrong\n"
        "BEST: A"))
    monkeypatch.setattr(proxy, "Runner", lambda **kw: runner)

    asyncio.run(proxy._run_shadow_trial(
        [{"role": "user", "content": "a question"}],
        "a question",
        "ollama:primary",
        "the primary answer",
        "t",
    ))

    assert len(runner.trials) == 3, runner.trials        # one call per arm
    assert len(runner.judges) == 1, "the batch must be judged once"  # ...one judge call

    s = Store(tmp_path / "p.db")
    rows = {dict(r)["deploy_id"]: dict(r) for r in s.conn.execute(
        "SELECT deploy_id, ok, signal_kind, meta FROM observations").fetchall()}
    s.close()

    assert len(rows) == 3
    assert all(r["signal_kind"] == "judge_trial" for r in rows.values())
    # The judge said PASS, PASS, FAIL. Which *arm* got which label depends on the
    # trial order (untried arms are shuffled), so assert the shape, not the names.
    assert sorted(r["ok"] for r in rows.values()) == [0, 1, 1]
    # The winner is recorded — what "choose the best performing model" needs to be
    # answerable after the fact — and it is exactly one arm, and a passing one.
    winners = [d for d, r in rows.items() if json.loads(r["meta"]).get("best")]
    assert len(winners) == 1
    assert rows[winners[0]]["ok"] == 1
