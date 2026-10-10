"""Chat web search: decide when a turn needs the live web, then ground the answer.

The scout's `_need_search` decides whether to search for one *model*; this decides
for one *chat turn*. Same shape, different question, and the same bias: a cheap
deterministic cue layer first, so the decision is instant, free and auditable
("point at the cue that fired"). An LLM tie-break can be layered on later without
changing the callers.

Search here is *grounding*, not a tool loop: the results are injected as a system
message before routing, so whichever arm the router picks answers from supplied
evidence and cites it. That keeps one request one model call, and it means a free
arm can answer a current-events question correctly.
"""
from __future__ import annotations

import re

# The answer changes with time, or the user is transparently asking for the web.
# Deliberately narrow: a false positive spends quota and adds latency to a turn
# that did not need it, and the escape hatch (an explicit `search: true`, or
# `MI_CHAT_SEARCH=on`) is one flag away.
_EXPLICIT_CUES = (
    "search the web", "search online", "web search", "look it up", "look up",
    "google", "find online", "browse the web", "on the web",
)
_TIME_CUES = (
    "latest", "current", "currently", "today", "tonight", "as of", "right now",
    "this week", "this month", "this year", "recent", "recently", "news",
    "breaking", "just announced", "up to date", "up-to-date", "release date",
    "changelog", "roadmap", "stock price", "exchange rate", "weather", "who won",
    "election results",
)
_YEAR = re.compile(r"\b(20[2-9]\d)\b")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)


def needs_search(prompt: str) -> tuple[bool, str]:
    """`(needs_web, reason)` for one prompt. Deterministic and explainable."""
    text = " " + " ".join((prompt or "").lower().split()) + " "
    for cue in _EXPLICIT_CUES:
        if cue in text:
            return True, f"explicit:{cue}"
    for cue in _TIME_CUES:
        if cue in text:
            return True, f"time:{cue}"
    if _YEAR.search(text):
        return True, "year"
    if _URL.search(text):
        return True, "url"
    return False, "no_cue"


def context_message(query: str, results: list[dict]) -> dict:
    """A system message carrying the search results, with their URLs to cite.

    Written as *evidence to use*, not as an instruction to obey: the model is told
    to cite and to admit when the results do not answer the question, so a thin
    set of hits produces a hedged answer rather than a confident invention.
    """
    lines = [
        "Live web search results are provided below. Use them to answer the "
        "user's question, cite the source URL(s) inline with markdown links, and "
        "state plainly if they do not contain the answer. Do not invent facts "
        "they do not support.",
        f"Search query: {query!r}",
    ]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"[{i}] {title}\n{url}\n{snippet}")
    return {"role": "system", "content": "\n\n".join(lines)}
