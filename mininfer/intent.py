"""Prompt -> task classification.

`auto` used to mean "the default task, always". That is a silent guess: a SQL
question routed as a code edit gets the wrong token budget (6000/900 instead of
1800/400) and the wrong capability gate (`tools` instead of `structured`), so it
is scored against constraints that do not describe it.

Two layers, cheapest first:

1. **cue matching** over the task profiles — deterministic, instant, free, and
   explainable. This is the default because it can be audited: you can point at
   the cue that fired.
2. **an injected LLM tie-break** — only when layer 1 is ambiguous. The callable is
   supplied by the caller so this module stays pure and testable, and so a
   specialised classifier (TypeSafe's Jev returns exactly `choices/scores/booleans`)
   can be dropped in without touching the router.

Every result carries `source` and per-task `scores`, because a wrong task is only
debuggable if you can see why it was picked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .schema import TaskProfile

# A single word must match on a boundary so `sql` does not fire inside `mysql8`;
# a multi-word cue is already specific enough to match as a substring.
_WORD_WEIGHT = 1.0
_PHRASE_WEIGHT = 1.5


@dataclass(frozen=True, slots=True)
class Intent:
    task: str
    confidence: float
    source: str  # cue | llm | default
    scores: dict[str, float] = field(default_factory=dict)

    def top(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.scores.items(), key=lambda kv: -kv[1])[:n]

    def as_dict(self) -> dict:
        return {
            "task": self.task,
            "confidence": self.confidence,
            "source": self.source,
            "scores": {k: round(v, 2) for k, v in self.top(4)},
        }


def _normalise(text: str) -> str:
    return " " + " ".join(text.lower().split()) + " "


def cue_scores(prompt: str, tasks: dict[str, TaskProfile]) -> dict[str, float]:
    """Sum cue weights per task. Empty when nothing matches."""
    text = _normalise(prompt)
    out: dict[str, float] = {}
    for name, profile in tasks.items():
        total = 0.0
        for cue in getattr(profile, "cues", ()) or ():
            c = cue.lower().strip()
            if not c:
                continue
            if " " in c:
                if c in text:
                    total += _PHRASE_WEIGHT
            elif re.search(rf"\b{re.escape(c)}\b", text):
                total += _WORD_WEIGHT
        if total:
            out[name] = total
    return out


def classify(
    prompt: str,
    tasks: dict[str, TaskProfile],
    *,
    default: str,
    llm: Callable[[str, list[str]], str | None] | None = None,
    min_confidence: float = 0.6,
) -> Intent:
    """Pick a task for `prompt`, falling back to `default` when nothing matches.

    `confidence` is the winner's share of all matched weight, so a prompt that
    trips one task strongly reads as confident and one that trips three equally
    reads as ambiguous. Only an ambiguous prompt is worth an LLM call.
    """
    if not tasks:
        return Intent(default, 0.0, "default", {})

    scores = cue_scores(prompt, tasks)
    total = sum(scores.values())
    if not total:
        picked = llm(prompt, list(tasks)) if llm is not None else None
        if picked in tasks:
            return Intent(picked, 0.0, "llm", {})
        return Intent(default, 0.0, "default", {})

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_score = ranked[0]
    confidence = top_score / total

    if confidence < min_confidence and llm is not None:
        shortlist = [name for name, _ in ranked[:4]] or list(tasks)
        picked = llm(prompt, shortlist)
        if picked in tasks:
            return Intent(picked, round(confidence, 3), "llm", scores)

    return Intent(top, round(confidence, 3), "cue", scores)


def last_user_text(messages: Iterable[dict]) -> str:
    """The most recent user turn — what the request is actually about."""
    for m in reversed(list(messages)):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


def merge_profiles(name: str, profiles: list[TaskProfile]) -> TaskProfile:
    """The conservative join of several candidate profiles.

    When a prompt could be either task, the safe answer is a model that satisfies
    *both* readings, priced for the larger of the two. So: max token budget, max
    context, the union of required capabilities, the stricter quality floor, and
    the union of leaderboards with their weights averaged.

    This is deliberately stricter than either input. An under-provisioned route
    fails silently (a model without `structured` returns prose you then parse and
    lose); an over-provisioned one just costs a little more.
    """
    if not profiles:
        raise ValueError("merge_profiles needs at least one profile")
    if len(profiles) == 1:
        return profiles[0]

    keys: dict[str, list[float]] = {}
    for p in profiles:
        for k in p.benchmark_keys:
            keys.setdefault(k, []).append(p.benchmark_weights.get(k, 1.0))

    return TaskProfile(
        name=name,
        tokens_in=max(p.tokens_in for p in profiles),
        tokens_out=max(p.tokens_out for p in profiles),
        require={c: True for p in profiles for c, needed in p.require.items() if needed},
        min_context=max(p.min_context for p in profiles),
        min_success_lb=max(p.min_success_lb for p in profiles),
        benchmark_keys=tuple(keys),
        benchmark_weights={k: sum(v) / len(v) for k, v in keys.items()},
        cues=tuple(dict.fromkeys(c for p in profiles for c in p.cues)),
        description="merged: " + " + ".join(p.name for p in profiles),
    )
