"""Judge evaluation for untried model trials and shadow requests.

Evaluates whether a model's output is acceptable — first with cheap deterministic
heuristics, then with an LLM judge when one is available.

**Judging is batched.** The judge is itself a model call, so judging *k* arms one
at a time costs *k* calls and *k* latencies, each re-reading the same prompt. One
call for the whole batch is cheaper and also a better question: "which of these is
best?" is more consistent than *k* independent "is this good?" verdicts, because
the candidates are compared against each other rather than against a bar each
call hallucinates separately.
"""
from __future__ import annotations

import logging
import re
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

#: Labels handed to the judge. Short tokens, not deploy ids: an id like
#: `openrouter/thinkingmachines/nvfp4:qwen/qwen3.8-27b:free` is easy to mangle when
#: echoed back, and a mangled echo would be read as "no verdict" and silently pass.
_LABELS = "ABCDEFGH"

_BATCH_SYSTEM = (
    "You are an impartial judge comparing candidate responses to a user prompt.\n"
    "Judge each candidate on whether it correctly and usefully addresses the prompt.\n"
    "Reply with exactly one line per candidate, in order:\n"
    "CANDIDATE A: PASS - <short reason>\n"
    "CANDIDATE B: FAIL - <short reason>\n"
    "Then one final line naming the best candidate:\n"
    "BEST: A\n"
    "Use only PASS or FAIL. If a reference response is given, treat it as the bar: a "
    "candidate that matches or beats it passes. BEST must name one of the candidates "
    "you marked PASS; write `BEST: none` if none passed."
)

_VERDICT_RE = re.compile(r"^\s*CANDIDATE\s+([A-Za-z0-9]+)\s*[:\-]\s*(PASS|FAIL)\b(.*)$",
                         re.IGNORECASE)
_BEST_RE = re.compile(r"^\s*BEST\s*[:\-]\s*([A-Za-z0-9]+)", re.IGNORECASE)


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


def _build_batch_prompt(prompt: str, live: list[tuple[str, str]],
                        reference_text: str | None) -> str:
    parts = [f"User prompt:\n{prompt[:800]}"]
    if reference_text:
        parts.append(f"Reference response (the quality bar):\n{reference_text[:800]}")
    for label, text in live:
        parts.append(f"Candidate {label}:\n{(text or '')[:1200]}")
    parts.append(
        "Judge every candidate above — one `CANDIDATE <label>: PASS|FAIL - <reason>` "
        "line each, then a final `BEST: <label>` line."
    )
    return "\n\n".join(parts)


def evaluate_batch(
    prompt: str,
    candidates: list[tuple[str, str]],
    *,
    reference_text: str | None = None,
    runner: "Runner | None" = None,
    judge_model: str | None = None,
) -> tuple[dict[str, tuple[bool, str]], str | None]:
    """Judge several candidate responses in **one** model call.

    `candidates` is `[(label, text)]`; labels are opaque and returned as keys, so
    the caller can map back to deploy ids.

    Returns `({label: (ok, reason)}, best_label | None)`.

    Heuristics run first and are free, so an empty or error-in-body candidate never
    costs a call. A judge that errors or answers unparseably never *penalises*: the
    candidate keeps its heuristic verdict, because a broken judge is not evidence
    about the model.
    """
    if not candidates:
        return {}, None

    verdicts: dict[str, tuple[bool, str]] = {}
    live: list[tuple[str, str]] = []
    for label, text in candidates:
        ok, reason = evaluate_heuristics(prompt, text)
        verdicts[label] = (ok, reason)
        if ok:
            live.append((label, text))

    first_live = live[0][0] if live else None
    if runner is None or not judge_model or not live:
        return verdicts, first_live

    try:
        res = runner(
            judge_model,
            [
                {"role": "system", "content": _BATCH_SYSTEM},
                {"role": "user", "content": _build_batch_prompt(prompt, live, reference_text)},
            ],
            max_tokens=80 + 48 * len(live),
            temperature=0.0,
            timeout=30.0,
        )
        if not res.ok or not (res.text or "").strip():
            return verdicts, first_live

        best: str | None = None
        for line in (res.text or "").splitlines():
            verdict = _VERDICT_RE.match(line)
            if verdict:
                label = verdict.group(1).upper()
                passed = verdict.group(2).upper() == "PASS"
                reason = (verdict.group(3) or "").strip(" \t-:.")
                if label in verdicts:
                    verdicts[label] = (
                        passed,
                        "judge_pass" if passed else f"judge_fail: {reason[:160]}",
                    )
                continue
            named = _BEST_RE.match(line)
            if named:
                candidate_best = named.group(1).upper()
                # A "best" the judge itself failed is not a winner: "choose the best
                # performing model" cannot mean "choose one that failed".
                if candidate_best in verdicts and verdicts[candidate_best][0]:
                    best = candidate_best

        # A missing `BEST:` line (or one naming an unknown label) falls back to the
        # first pass in the order given — never to "no winner" when one passed.
        if best is None:
            best = next((label for label, _ in candidates if verdicts[label][0]), None)
        return verdicts, best
    except Exception as exc:  # noqa: BLE001 - a judge fault is not model evidence
        logger.warning("Batch judge exception: %s", exc)
        return verdicts, first_live


def evaluate_trial_response(
    prompt: str,
    candidate_text: str,
    *,
    reference_text: str | None = None,
    runner: "Runner | None" = None,
    judge_model: str | None = None,
) -> tuple[bool, str]:
    """Judge one candidate. A batch of one, so a single path can never drift."""
    verdicts, _ = evaluate_batch(
        prompt, [("A", candidate_text)], reference_text=reference_text,
        runner=runner, judge_model=judge_model)
    return verdicts.get("A", (True, "heuristics_passed"))
