"""Intent classification: the layer that turns `auto` into a decision.

Before this, `auto` meant `MI_DEFAULT_TASK` unconditionally, so a SQL question
was scored against a code edit's token budget and capability gate. The tests below
pin the two properties that matter: cue matching is explainable, and an LLM is
only consulted when the cues genuinely disagree.
"""
from __future__ import annotations

import pytest

from mininfer.intent import classify, cue_scores, last_user_text, merge_profiles
from mininfer.schema import TaskProfile


def _task(name: str, cues: tuple[str, ...]) -> TaskProfile:
    return TaskProfile(name=name, tokens_in=1, tokens_out=1, cues=cues)


TASKS = {
    "sql_generation": _task("sql_generation", ("sql", "select from", "group by")),
    "code_edit": _task("code_edit", ("refactor", "function", "traceback")),
    "summarise": _task("summarise", ("summarise", "tl;dr")),
}


def test_single_cue_picks_the_task():
    r = classify("Write a SQL query for me", TASKS, default="code_edit")
    assert r.task == "sql_generation"
    assert r.source == "cue"
    assert r.confidence == 1.0


def test_single_words_match_on_a_boundary_but_phrases_do_not_need_one():
    # `sql` must not fire inside `mysql`; a phrase like `group by` is specific
    # enough to match as a substring.
    scores = cue_scores("connect to mysql and group by region", TASKS)
    assert "sql_generation" in scores          # from "group by"
    assert "code_edit" not in scores
    assert cue_scores("mysql only", TASKS) == {}


def test_phrase_is_weighted_above_a_bare_word():
    only_word = cue_scores("sql", TASKS)["sql_generation"]
    only_phrase = cue_scores("group by", TASKS)["sql_generation"]
    assert only_phrase > only_word


def test_ambiguity_is_reported_not_hidden():
    """Cues disagree -> still return the best match, but say so.

    Forcing the default here would be worse than useless: the prompt says "sql",
    so discarding that to answer `code_edit` loses real signal. `confidence` is
    what tells the caller the choice was close.
    """
    r = classify("refactor the sql", TASKS, default="code_edit")
    assert r.confidence == 0.5                 # 1.0 vs 1.0
    assert r.source == "cue"                   # it *was* a cue match
    assert r.task in ("sql_generation", "code_edit")
    assert len(r.top()) == 2


def test_no_cue_at_all_falls_back_to_the_default():
    r = classify("hello there", TASKS, default="summarise")
    assert r.task == "summarise"
    assert r.source == "default"
    assert r.scores == {}


def test_llm_is_consulted_only_when_the_cues_disagree():
    calls: list[list[str]] = []

    def llm(_prompt: str, candidates: list[str]) -> str | None:
        calls.append(candidates)
        return "sql_generation"

    clear = classify("please summarise this document", TASKS,
                     default="code_edit", llm=llm)
    assert clear.source == "cue"
    assert calls == []                         # confident -> no model call

    murky = classify("refactor the sql", TASKS, default="code_edit", llm=llm)
    assert calls, "an ambiguous prompt must consult the tie-break"
    assert murky.task == "sql_generation"
    assert murky.source == "llm"


def test_llm_answering_off_menu_keeps_the_best_cue():
    consulted: list[bool] = []

    def llm(_prompt: str, _candidates: list[str]) -> str | None:
        consulted.append(True)
        return "not_a_task"

    r = classify("refactor the sql", TASKS, default="code_edit", llm=llm)
    assert consulted, "an ambiguous prompt should still consult the tie-break"
    assert r.source == "cue"                   # the bad answer is discarded
    assert r.task in ("sql_generation", "code_edit")


def test_llm_alone_handles_a_prompt_with_no_cues():
    r = classify("what is the meaning of this", TASKS, default="code_edit",
                 llm=lambda *_a: "summarise")
    assert r.task == "summarise"
    assert r.source == "llm"


def test_last_user_text_ignores_system_and_assistant_turns():
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert last_user_text(messages) == "second"
    assert last_user_text([]) == ""


def test_as_dict_exposes_why_the_task_was_chosen():
    d = classify("write a sql query", TASKS, default="code_edit").as_dict()
    assert d["task"] == "sql_generation"
    assert d["source"] == "cue"
    assert "sql_generation" in d["scores"]


# --- merge_profiles: the conservative join used when cues disagree -------------

def _profile(name, *, tin, tout, require, ctx, floor, keys, weights):
    return TaskProfile(
        name=name, tokens_in=tin, tokens_out=tout, require=require,
        min_context=ctx, min_success_lb=floor,
        benchmark_keys=tuple(keys), benchmark_weights=weights,
    )


A = _profile("alpha", tin=100, tout=10, require={"structured": True}, ctx=8000,
             floor=0.2, keys=["coding"], weights={"coding": 2.0})
B = _profile("beta", tin=900, tout=90, require={"tools": True}, ctx=32000,
             floor=0.6, keys=["coding", "aa_intelligence"],
             weights={"coding": 1.0, "aa_intelligence": 4.0})


def test_merge_takes_the_larger_budget_and_stricter_floor():
    m = merge_profiles("alpha+beta", [A, B])
    assert m.tokens_in == 900          # max, so an under-provisioned route is impossible
    assert m.tokens_out == 90
    assert m.min_context == 32000
    assert m.min_success_lb == 0.6     # strictest


def test_merge_unions_required_capabilities():
    m = merge_profiles("alpha+beta", [A, B])
    # a model must satisfy *both* readings, not either
    assert m.require == {"structured": True, "tools": True}


def test_merge_averages_shared_leaderboard_weights_and_unions_keys():
    m = merge_profiles("alpha+beta", [A, B])
    assert set(m.benchmark_keys) == {"coding", "aa_intelligence"}
    assert m.benchmark_weights["coding"] == 1.5            # (2.0 + 1.0) / 2
    assert m.benchmark_weights["aa_intelligence"] == 4.0   # only one side lists it


def test_merge_of_one_profile_is_that_profile():
    assert merge_profiles("alpha", [A]) is A


# --------------------------------------------------------------------------- #
# the catch-all, tested against the shipped policy rather than a fixture
# --------------------------------------------------------------------------- #
# `general_chat` exists because a prompt that trips no cue used to fall through
# to `code_edit` — 32K context, 0.55 quality floor, tool support required. These
# load `config/policy.yaml` itself, so a cue edit that breaks routing fails here.


def _shipped():
    import pathlib
    from mininfer.router import DEFAULT_TASK, Policy

    root = pathlib.Path(__file__).resolve().parent.parent
    return Policy.load(root / "config" / "policy.yaml"), DEFAULT_TASK


def test_the_default_task_is_conversational_not_code():
    """The default is what a prompt that matched *nothing* gets, and such a prompt
    is by definition not obviously a coding task."""
    _, default = _shipped()
    assert default == "general_chat"


@pytest.mark.parametrize("prompt", [
    "Hello, how are you?", "hi", "thanks!", "tell me a joke",
    "write me a poem about the sea", "can you explain this error",
])
def test_conversational_prompts_are_classified_not_merely_defaulted(prompt):
    """`source=cue` and confidence 1.0 — not `source=default`.

    The distinction is the whole point: falling back to the right task by accident
    leaves `intent.confidence` at 0.0 and tells the decision log nothing.
    """
    (_, tasks), default = _shipped()
    r = classify(prompt, tasks, default=default)
    assert r.task == "general_chat"
    assert r.source == "cue", f"{prompt!r} fell through instead of matching"


@pytest.mark.parametrize("prompt,expected", [
    ("fix the type error in parse_config", "code_edit"),
    ("refactor this function", "code_edit"),
    ("write me a query to get the top 10 customers by revenue", "sql_generation"),
    ("select * from users", "sql_generation"),
    ("summarise this article", "summarise"),
    ("give me the tl;dr", "summarise"),
    ("extract the invoice fields into json", "extraction"),
    ("search the web and plan the steps", "agent_tools"),
    ("prove that sqrt 2 is irrational", "hard_reasoning"),
    ("audit the shelf photo", "shelf_image_audit"),
])
def test_the_catch_all_does_not_steal_another_tasks_prompts(prompt, expected):
    """A catch-all that eats another task's cues is a regression, not a catch-all.

    `write` was in the cue list and took "write me a query for the top 10
    customers" away from `sql_generation`, 1.00 -> 0.50 on the wrong task.
    """
    (_, tasks), default = _shipped()
    r = classify(prompt, tasks, default=default)
    assert r.task == expected, f"{prompt!r} -> {r.task} (wanted {expected})"
    assert r.confidence == 1.0


def test_an_unmatched_prompt_still_falls_through_to_the_catch_all():
    (_, tasks), default = _shipped()
    r = classify("asdfgh qwerty zxcvb", tasks, default=default)
    assert r.task == "general_chat" and r.source == "default"
