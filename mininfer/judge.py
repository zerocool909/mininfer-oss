"""Judge evaluation for untried model trials and shadow requests.

Evaluates whether an untried model's output is acceptable via fast heuristic
checks and optional LLM-as-a-judge comparison against a reference response.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .execute import Runner

logger = logging.getLogger("mininfer.judge")

# Phrases that indicate provider errors or degenerate output returned as 200 text
_DISQUALIFYING_SUBSTRINGS = (
    "\"error\": {\"message\":",
    "\"error\":{\"message\":",
    "invalid api key",
    "incorrect api key",
    "authentication failed",
    "rate limit exceeded",
    "resource has been exhausted",
    "service temporarily unavailable",
    "internal server error",
)


def evaluate_heuristics(prompt: str, candidate_text: str) -> tuple[bool, str]:
    """Fast deterministic heuristics on candidate text."""
    text = (candidate_text or "").strip()
    if not text:
        return False, "empty_response"

    # Minimal reasonable length unless prompt was exceptionally brief
    if len(text) < 10 and len(prompt.strip()) > 15:
        return False, "response_too_short"

    lower = text.lower()
    for phrase in _DISQUALIFYING_SUBSTRINGS:
        if phrase in lower:
            return False, f"error_in_body: {phrase}"

    # Repetition loop detection: check if any line repeats 6+ times consecutively
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    repeat_count = 1
    prev_line = ""
    for line in lines:
        if line == prev_line:
            repeat_count += 1
            if repeat_count >= 6:
                return False, "repetitive_collapse_loop"
        else:
            prev_line = line
            repeat_count = 1

    return True, "heuristics_passed"


def evaluate_trial_response(
    prompt: str,
    candidate_text: str,
    *,
    reference_text: str | None = None,
    runner: Runner | None = None,
    judge_model: str | None = None,
) -> tuple[bool, str]:
    """Judge if candidate_text is a successful response to prompt.

    Returns (ok: bool, reason: str).
    """
    ok_heur, heur_reason = evaluate_heuristics(prompt, candidate_text)
    if not ok_heur:
        return False, heur_reason

    # If no LLM runner or judge model available, heuristic pass is sufficient
    if runner is None or not judge_model:
        return True, heur_reason

    # Run lightweight LLM evaluation
    try:
        judge_messages = [
            {
                "role": "system",
                "content": (
                    "You are an impartial judge evaluating an AI model response. "
                    "Determine if the candidate response reasonably addresses the user's prompt. "
                    "Reply on the first line with either 'JUDGMENT: PASS' or 'JUDGMENT: FAIL', "
                    "followed by a concise reason."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User Prompt:\n{prompt[:800]}\n\n"
                    f"Candidate Response:\n{candidate_text[:1200]}\n\n"
                    + (f"Reference Response:\n{reference_text[:800]}\n\n" if reference_text else "")
                    + "Does the candidate response reasonably address the request?"
                ),
            },
        ]
        res = runner(judge_model, judge_messages, max_tokens=100, temperature=0.0, timeout=20.0)
        if not res.ok or not (res.text or "").strip():
            # If judge call failed, don't penalize the candidate if heuristics passed
            return True, f"heuristics_pass (judge_error: {res.error_class})"

        first_line = (res.text or "").strip().splitlines()[0].upper()
        if "FAIL" in first_line:
            explanation = (res.text or "").strip().replace("\n", " ")[:200]
            return False, f"judge_fail: {explanation}"
        elif "PASS" in first_line:
            return True, "judge_pass"
        else:
            # Ambiguous output; default to heuristic pass
            return True, "judge_ambiguous_pass"
    except Exception as exc:
        logger.warning(f"Judge evaluation exception: {exc}")
        return True, f"heuristics_pass (judge_exception: {exc})"
