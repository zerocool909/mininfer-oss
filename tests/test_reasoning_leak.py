"""A reasoning model's visible thinking is not the caller's answer.

Several providers (Groq's qwen builds among them) put the model's thinking
*in the answer*, not in a separate field. So a caller who asked for "just the
number" could receive a truncated think trace — with the number nowhere in it.

Found live: the answer to "what is the sum of 1 to 100" came back as
`"\n<think>\nThe user is asking me to reply with"` and the check that looks
for `5050` failed. The arm did not fail; it *answered something else*.
"""
from __future__ import annotations

import pytest

from mininfer.execute import CallResult, strip_reasoning
from mininfer.proxy import try_fallbacks
from mininfer.store import Store


@pytest.mark.parametrize("raw,expected", [
    # A complete block is removed; the answer after it survives.
    ("<think>the user wants a number</think>5050", "5050"),
    # An unterminated block (truncated by max_tokens) is removed to the end.
    ("<think>the user is asking me to reply with", ""),
    # Nothing to remove: the text is untouched.
    ("5050", "5050"),
    ("", ""),
    # The tag is matched case-insensitively.
    ("<THINK>hmm</THINK>42", "42"),
    # Text before the block is kept.
    ("5050<think>and here is my reasoning</think>", "5050"),
])
def test_the_reasoning_block_is_removed_from_the_answer(raw, expected):
    assert strip_reasoning(raw).strip() == expected.strip()


def test_a_think_only_answer_is_treated_as_no_content(tmp_path):
    """A block with no answer after it is an *empty* answer.

    Which is what the fallback chain needs to know: the arm produced nothing
    usable, so the next arm is tried — and the observation says why.
    """
    store = Store(tmp_path / "p.db")

    def runner(deploy_id, messages, **kw):
        if deploy_id == "a:1":
            return CallResult("a:1", text="<think>still thinking", ok=True,
                              tokens_in=5, tokens_out=20)
        return CallResult("b:2", text="5050", ok=True, tokens_in=5, tokens_out=3)

    selected, res, attempts = try_fallbacks(
        ["a:1", "b:2"], [{"role": "user", "content": "sum 1 to 100"}], runner,
        task_name="general_chat", store=store)

    # The think-only answer was not served...
    assert selected == "b:2"
    assert res.text == "5050"
    # ...and it was recorded as the failure it was.
    assert attempts[0].error_class == "empty_content"
    rows = store.conn.execute(
        "SELECT deploy_id, ok, error_class FROM observations ORDER BY id").fetchall()
    assert [(r["deploy_id"], r["ok"], r["error_class"]) for r in rows] == [
        ("a:1", 0, "empty_content"), ("b:2", 1, None)]
    store.close()
