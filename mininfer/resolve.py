"""Entity resolution.

One website says `Qwen3-30B-A3B`, another `qwen/qwen3-30b-a3b-instruct`,
the provider API says `qwen3-30b-a3b-instruct-2507`, and OpenRouter hands us
`hugging_face_id: Qwen/Qwen3-30B-A3B-Instruct-2507`.

Because `weights_id_for()` prefers the HF repo, every source that reports one
already agrees. The work that remains is joining sources that *don't*:

  * **exact** — both sides normalise to the same string. Merged automatically.
  * **near** — high similarity. Never auto-merged: proposed for review.

Two rules learned the hard way:

1. **Parameter count is identity.** An earlier version stripped param tokens to
   make `...-550b-a55b-bf16` match `...-550b-a55b-nvfp4`, which also made
   `llama-3.1-8b` match `llama-3.1-70b`. `normalize()` already removes the
   *serving* suffix (bf16/fp8/nvfp4/awq/...), so the full normalised name is the
   correct key on its own.
2. **A bad merge is worse than no merge.** A missed merge costs a duplicate row
   and some benchmark coverage. A wrong merge silently attaches one model's
   benchmarks and outcomes to another, and nothing downstream can detect it. So
   merges are guarded and refusals are recorded, not swallowed.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .normalize import normalize, params_b_from_name
from .store import Store

# Similarity at or above this is proposed for review. Nothing is auto-merged
# except an exact normalised-name match.
WEAK = 0.92

_PARAM_TOKEN = re.compile(r"\d+(?:\.\d+)?b", re.I)


def _key(display_name: str, hf_repo: str | None) -> str:
    """Comparison key = normalised name, param count retained."""
    return normalize(hf_repo.split("/")[-1] if hf_repo else display_name)


def _params(name: str) -> float | None:
    return params_b_from_name(name)


@dataclass(slots=True)
class Proposal:
    alias_id: str
    canonical_id: str
    alias_name: str
    canonical_name: str
    similarity: float
    reason: str


def propose(store: Store, *, auto: bool = True, near_window: int = 20) -> tuple[list[Proposal], list[Proposal]]:
    """Returns (merged, needs_review). Only exact matches are merged when auto."""
    rows = store.unstaged_weights()
    merged: list[Proposal] = []
    review: list[Proposal] = []

    def rank(r: dict) -> tuple:
        # HF-keyed rows carry the strongest identity, so they win as canonical.
        return (0 if r["weights_id"].startswith("hf:") else 1, -len(r["weights_id"]))

    ordered = sorted(rows, key=rank)
    canonical_for: dict[str, str] = {}
    name_for: dict[str, str] = {}

    for r in ordered:
        wid = r["weights_id"]
        if not store.weights_exists(wid):
            continue  # merged away earlier in this pass
        k = _key(r["display_name"], r["hf_repo"])
        if not k:
            continue
        if (canonical := canonical_for.get(k)) is None:
            canonical_for[k] = wid
            name_for[k] = r["display_name"]
            continue

        p = Proposal(wid, canonical, r["display_name"], name_for[k], 1.0,
                     f"normalised name identical ({k})")
        if auto:
            moved = store.merge_weights(wid, canonical, p.reason, 1.0)
            if moved or store.merged_ok(wid):
                merged.append(p)
            else:
                p.reason = "REFUSED by merge guard — see quarantine"
                review.append(p)
        else:
            review.append(p)

    # Near matches are proposed only, and never auto-merged.
    #
    # A full pairwise scan is O(K^2): at the real registry size (K~21k) that is
    # ~229M difflib comparisons and it never finishes. Similar names sort
    # adjacently, so we compare each key with the next `near_window` keys instead
    # — O(K*window) — and drop pairs whose length ratio makes 0.92 unreachable (a
    # *provable* prune: ratio >= 0.92 implies min/max length >= 0.852). A missed
    # near match costs a duplicate row, never a wrong merge — the right trade.
    keys = sorted(canonical_for)
    for i, k in enumerate(keys):
        pk = _params(k)
        for other in keys[i + 1: i + 1 + near_window]:
            po = _params(other)
            if pk is not None and po is not None and abs(pk - po) > 0.01:
                continue
            hi, lo = max(len(k), len(other)), min(len(k), len(other))
            if hi and lo / hi < 0.85:
                continue
            sim = difflib.SequenceMatcher(None, k, other).ratio()
            if sim >= WEAK:
                review.append(Proposal(
                    canonical_for[k], canonical_for[other], k, other, round(sim, 4),
                    "similar normalised names — needs human or LLM confirmation",
                ))
    return merged, review


def review_payload(review: list[Proposal]) -> list[dict]:
    """Shape for an LLM adjudicator or an admin UI."""
    return [
        {"alias_id": p.alias_id, "candidate_id": p.canonical_id,
         "left": p.alias_name, "right": p.canonical_name,
         "similarity": p.similarity, "reason": p.reason}
        for p in review
    ]
