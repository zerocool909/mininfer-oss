"""Leaderboard registry — one place that knows what a benchmark key *is*.

Scores live flat on `weights.benchmark` (`{"coding": 71.9, "design_arena_elo":
1327.5}`). Keeping them flat keeps ingestion and the router simple, and it makes
provenance readable without a join: **the key names the leaderboard**. This module
turns a key into the label, the source, and the direction of the metric, so the
router can weight several leaderboards and the UI can tag each score with where it
came from.

Adding a leaderboard is data-only — add an entry here and map it in the adapter.
`benchmark_norms()` in the router normalises each key by its own p5/p95, so a new
source needs no unit conversion even when its scale is arbitrary (AA index 0-100
vs arena Elo ~1000-1500).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Leaderboard:
    key: str
    label: str                 # the metric within the leaderboard
    source: str                # the leaderboard itself
    url: str = ""
    higher_is_better: bool = True


BOARDS: dict[str, Leaderboard] = {
    "aa_intelligence": Leaderboard(
        "aa_intelligence", "Intelligence Index", "Artificial Analysis",
        "https://artificialanalysis.ai/"),
    "coding": Leaderboard(
        "coding", "Coding Index", "Artificial Analysis",
        "https://artificialanalysis.ai/"),
    "agentic": Leaderboard(
        "agentic", "Agentic Index", "Artificial Analysis",
        "https://artificialanalysis.ai/"),
    "design_arena_elo": Leaderboard(
        "design_arena_elo", "Elo (mean across categories)", "Design Arena",
        "https://www.designarena.ai/"),
}

# Provenance recorded on the `weights` row for each source, matched by prefix.
SOURCE_BY_KEY: dict[str, str] = {
    "aa_intelligence": "openrouter/artificial_analysis",
    "coding": "openrouter/artificial_analysis",
    "agentic": "openrouter/artificial_analysis",
    "design_arena_elo": "openrouter/design_arena",
}


def describe(key: str) -> Leaderboard:
    """Registry entry for a key, or a readable fallback for an unmapped one."""
    return BOARDS.get(key) or Leaderboard(key, key.replace("_", " "), "unknown")


def tags(keys, scores: dict | None = None) -> list[dict]:
    """Per-leaderboard tags for a response payload, newest sources included.

    `scores` is optional; when given, the raw value rides along so a caller can
    show the number next to the leaderboard it came from.
    """
    out: list[dict] = []
    for k in keys:
        b = describe(k)
        tag = {"key": k, "label": b.label, "source": b.source, "url": b.url}
        if scores is not None and scores.get(k) is not None:
            tag["value"] = scores[k]
        out.append(tag)
    return out
