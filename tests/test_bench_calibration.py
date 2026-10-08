"""The graded bench: difficulty labels and the routed sweep.

`floor_delta` and `effort_shrink_k` shipped as placeholders. Calibrating them needs
two things this pins: every bench task carries a hand-labelled difficulty, and the
harness can score the *decision* per difficulty — because an average is exactly what
hides "cheap arm is great at easy prompts and bad at hard ones".
"""
from __future__ import annotations

import pytest

from mininfer import bench as B
from mininfer.execute import CallResult
from mininfer.router import Policy
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

FAMILY = "reasoning"


def test_every_task_is_graded_and_the_labels_are_valid():
    levels = {"low", "medium", "high"}
    assert len(B.DIFFICULTY_BY_ID) == len(B.TASKS)
    unlabelled = [t.task_id for t in B.TASKS if t.task_id not in B.DIFFICULTY_BY_ID]
    assert not unlabelled, unlabelled
    assert {t.difficulty for t in B.TASKS} <= levels
    # A graded set where everything is one level measures nothing.
    assert len({t.difficulty for t in B.TASKS}) >= 3


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "p.db")
    s.upsert_weights(Weights("hf:anchor", "anchor", params_b=1.0,
                             benchmark={"aa_intelligence": 10.0, "coding": 10.0}))
    s.upsert_weights(Weights("hf:m", "m", params_b=7.0,
                             benchmark={"aa_intelligence": 90.0, "coding": 90.0}))
    s.upsert_deployment(Deployment("prov:m", "hf:m", "prov", "m",
                                   price_in=1.0, price_out=1.0, context_window=64000,
                                   caps={"structured": True, "tools": True}))
    s.commit()
    return s


def _tasks() -> dict:
    pol, tasks = Policy.load("config/policy.yaml")
    pol.require_callable = False
    return tasks


def permissive_policy() -> Policy:
    pol, _ = Policy.load("config/policy.yaml")
    pol.require_callable = False
    return pol


def _gold_runner(family: str):
    """Answers every task with its own gold answer, so the run is deterministic."""
    def run(deploy_id, messages, **kw):
        asked = messages[-1]["content"]
        for t in B.TASKS_BY_FAMILY[family]:
            if t.prompt == asked:
                return CallResult(deploy_id, text=B.gold_answer(t), ok=True,
                                  tokens_in=t.tokens_in, tokens_out=t.tokens_out)
        return CallResult(deploy_id, text="", ok=False, error_class="bad_output")
    return run


def test_run_routed_bench_reports_per_difficulty(tmp_path):
    s = _store(tmp_path)
    report = B.run_routed_bench(s, FAMILY, _gold_runner(FAMILY),
                                policy=permissive_policy(), tasks_by_name=_tasks(),
                                floor_delta=0.08, write=False)
    by = report.by_difficulty()
    assert set(by) <= {"low", "medium", "high"}
    assert sum(a["n"] for a in by.values()) == len(B.TASKS_BY_FAMILY[FAMILY])
    # Every answer was the gold one, so every difficulty is 100%.
    assert all(a["success_rate"] == 1.0 for a in by.values()), by
    assert report.summary()["cost_per_success"] is not None
    s.close()


def test_a_routed_bench_run_feeds_the_loop_it_calibrates(tmp_path):
    """`write=True` has to tag `effort`, or the sweep cannot learn from itself."""
    s = _store(tmp_path)
    B.run_routed_bench(s, FAMILY, _gold_runner(FAMILY), policy=permissive_policy(),
                       tasks_by_name=_tasks(), floor_delta=0.08, write=True)
    efforts = {r["effort"] for r in
               s.conn.execute("SELECT effort FROM observations").fetchall()}
    assert efforts and None not in efforts, efforts
    s.close()


class _FakeReport:
    def __init__(self, summary):
        self._summary = summary

    def summary(self):
        return self._summary


def _patch_runs(monkeypatch, table):
    monkeypatch.setattr(B, "run_routed_bench",
                        lambda *a, **k: _FakeReport(table[k["floor_delta"]]))


def test_calibration_prefers_the_cheapest_delta_on_a_tie(monkeypatch):
    table = {
        0.0: {"floor_delta": 0.0, "success_rate": 0.80, "cost_per_success": 0.010},
        0.08: {"floor_delta": 0.08, "success_rate": 0.80, "cost_per_success": 0.020},
        0.16: {"floor_delta": 0.16, "success_rate": 0.80, "cost_per_success": 0.030},
    }
    _patch_runs(monkeypatch, table)
    out = B.calibrate_floor_delta(None, FAMILY, None, policy=None,
                                  tasks_by_name={}, grid=(0.0, 0.08, 0.16))
    assert out["recommended"]["floor_delta"] == 0.0


def test_calibration_will_not_buy_accuracy_with_cost(monkeypatch):
    """Higher success alone must not win: it has to be cheaper *or* equal."""
    table = {
        0.0: {"floor_delta": 0.0, "success_rate": 0.80, "cost_per_success": 0.010},
        0.16: {"floor_delta": 0.16, "success_rate": 0.90, "cost_per_success": 0.100},
    }
    _patch_runs(monkeypatch, table)
    out = B.calibrate_floor_delta(None, FAMILY, None, policy=None,
                                  tasks_by_name={}, grid=(0.0, 0.16))
    assert out["recommended"]["floor_delta"] == 0.16, "the best success wins outright"

    # With a tolerance, a slightly worse but much cheaper delta is the answer.
    out = B.calibrate_floor_delta(None, FAMILY, None, policy=None, tasks_by_name={},
                                  grid=(0.0, 0.16), tolerance=0.15)
    assert out["recommended"]["floor_delta"] == 0.0


def test_calibration_without_a_success_recommends_nothing(monkeypatch):
    table = {
        0.0: {"floor_delta": 0.0, "success_rate": 0.0, "cost_per_success": None},
        0.08: {"floor_delta": 0.08, "success_rate": 0.0, "cost_per_success": None},
    }
    _patch_runs(monkeypatch, table)
    out = B.calibrate_floor_delta(None, FAMILY, None, policy=None,
                                  tasks_by_name={}, grid=(0.0, 0.08))
    assert out["recommended"] is None


def test_a_delta_that_breaks_hard_prompts_is_visible_in_the_report(tmp_path):
    """The per-difficulty split is the point: an average would hide this.

    `sql` is the family with all three levels, and the first six tasks span low and
    high, so the two must disagree — if they cannot, the split is decorative.
    """
    s = _store(tmp_path)
    family = "sql"

    def runner(deploy_id, messages, **kw):
        asked = messages[-1]["content"]
        for t in B.TASKS_BY_FAMILY[family]:
            if t.prompt == asked:
                if t.difficulty == "high":
                    return CallResult(deploy_id, text="not sql at all", ok=True,
                                      tokens_in=t.tokens_in, tokens_out=t.tokens_out)
                return CallResult(deploy_id, text=B.gold_answer(t), ok=True,
                                  tokens_in=t.tokens_in, tokens_out=t.tokens_out)
        return CallResult(deploy_id, text="", ok=False, error_class="bad_output")

    by = B.run_routed_bench(s, family, runner, policy=permissive_policy(),
                            tasks_by_name=_tasks(), floor_delta=0.08, limit=6,
                            write=False).by_difficulty()
    assert by["low"]["success_rate"] == 1.0, by
    assert by["high"]["success_rate"] == 0.0, by
    s.close()


def test_the_sweep_says_when_it_cannot_answer(tmp_path):
    """The guard that matters most.

    Every `sql` prompt here classifies *without* `needs_reasoning`, so `floor_delta`
    is inert across the whole family and each delta returns identical rows. That has
    to be reported as "cannot answer", because five tidy identical rows otherwise
    read as "the floor does not matter" — the opposite of the truth.
    """
    s = _store(tmp_path)
    rep = B.run_routed_bench(s, "sql", _gold_runner("sql"), policy=permissive_policy(),
                             tasks_by_name=_tasks(), floor_delta=0.08, write=False)
    assert rep.summary()["exercises_delta"] == 0
    s.close()


def test_the_sweep_reports_how_much_of_the_set_exercises_the_delta(tmp_path):
    s = _store(tmp_path)
    rep = B.run_routed_bench(s, "reasoning", _gold_runner("reasoning"),
                             policy=permissive_policy(), tasks_by_name=_tasks(),
                             floor_delta=0.08, write=False)
    summary = rep.summary()
    assert summary["exercises_delta"] == sum(1 for r in rep.rows if r.needs_reasoning)
    assert summary["exercises_delta"] >= 1, \
        "the reasoning set should contain at least one needs_reasoning prompt"
    s.close()
