"""Resolver LLM adjudicator — the reviewer half of entity resolution (Phase 5).

`resolve.propose()` is deliberately conservative: only an *exact* normalised-name
match is auto-merged, and high-similarity near matches are merely proposed
("a bad merge is worse than no merge"). This module closes that loop. A small
LangGraph agent reads each near-match pair with its metadata and decides whether
the two entries are the same weights.

The deterministic guard always runs last. Even when the model says "same", the
merge only happens if parameter counts agree — `Store.merge_weights` refuses
otherwise and records the refusal in `quarantine`. The LLM can widen what we
*consider*; it can never override the safety check.
"""
from __future__ import annotations

import json
import pathlib
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agent_ingest import _agent_llm, _parse_json
from .store import Store

_DEFAULT_MIN_CONFIDENCE = 0.7
_BATCH = 30

_RULES = """You are deduplicating an AI model registry. For each pair, decide whether \
the two entries are the SAME underlying weights (artifact) or DIFFERENT.

- Serving/quantization differences are the SAME: bf16, fp8, nvfp4, awq, gptq, gguf,
  turbo, TEE, -free, -nitro are hosting choices, not different models.
- A different parameter count is DIFFERENT.
- Different fine-tunes or variants are DIFFERENT: -thinking, -instruct vs -base,
  -code vs -chat, and different release dates (e.g. -2507 vs -2508).
- Different model families are DIFFERENT.
- When unsure, answer same=false. A wrong merge is worse than a duplicate."""


class ResolveState(TypedDict, total=False):
    pairs: list[dict]
    total_proposals: int
    agent_model: str
    db: str
    dry_run: bool
    min_confidence: float
    decisions: list[dict]
    merged: list[dict]
    kept: list[dict]
    refused: list[dict]
    error: str
    report: dict


# --------------------------------------------------------------------------- #
# pair construction
# --------------------------------------------------------------------------- #


def _meta(store: Store, weights_id: str) -> dict:
    row = store.weights_meta(weights_id)
    if row is None:
        return {"weights_id": weights_id, "display_name": weights_id}
    return {"weights_id": weights_id, "display_name": row["display_name"],
            "params_b": row["params_b"], "hf_repo": row["hf_repo"], "family": row["family"]}


def _pairs_from_proposals(store: Store, proposals: list) -> list[dict]:
    pairs = []
    for i, p in enumerate(proposals, start=1):
        pairs.append({
            "id": i,
            "alias_id": p.alias_id,
            "canonical_id": p.canonical_id,
            "similarity": float(p.similarity),
            "left": _meta(store, p.alias_id),
            "right": _meta(store, p.canonical_id),
        })
    return pairs


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# --------------------------------------------------------------------------- #
# LLM decision
# --------------------------------------------------------------------------- #


def _prompt(pairs: list[dict]) -> str:
    compact = [{"id": p["id"], "similarity": round(p["similarity"], 4),
                "left": p["left"], "right": p["right"]} for p in pairs]
    return (_RULES + "\n\nPairs:\n" + json.dumps(compact, ensure_ascii=False)
            + "\n\nReturn ONLY a JSON array:\n"
              '[{"id": 1, "same": true, "confidence": 0.9, "reason": "..."}]')


def _conf(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _decide(pairs: list[dict], agent_model: str | None) -> list[dict]:
    llm = _agent_llm(agent_model)
    resp = llm.invoke(_prompt(pairs))
    content = resp.content if hasattr(resp, "content") else str(resp)
    data = _parse_json(content)
    if not isinstance(data, list):
        raise ValueError("adjudicator did not return a JSON array")
    out = []
    for d in data:
        if isinstance(d, dict) and d.get("id") is not None:
            out.append({"id": d["id"], "same": bool(d.get("same")),
                        "confidence": _conf(d.get("confidence")),
                        "reason": str(d.get("reason") or "")})
    return out


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #


def adjudicate_node(state: ResolveState) -> dict:
    pairs = state.get("pairs", [])
    if not pairs:
        return {"decisions": []}
    try:
        decisions: list[dict] = []
        for chunk in _chunks(pairs, _BATCH):
            decisions.extend(_decide(chunk, state.get("agent_model")))
        return {"decisions": decisions}
    except Exception as exc:
        return {"error": f"adjudicate: {type(exc).__name__}: {exc}"}


def apply_node(state: ResolveState) -> dict:
    store = Store(state.get("db") or "mininfer.db")   # may be a path or a DSN
    by_id = {p["id"]: p for p in state.get("pairs", [])}
    min_conf = float(state.get("min_confidence", _DEFAULT_MIN_CONFIDENCE))
    merged, kept, refused = [], [], []

    for d in state.get("decisions", []):
        p = by_id.get(d["id"])
        if p is None:
            continue
        rec = {"id": d["id"], "alias_id": p["alias_id"], "canonical_id": p["canonical_id"],
               "left": p["left"].get("display_name"), "right": p["right"].get("display_name"),
               "confidence": d["confidence"], "reason": d["reason"]}
        if not d["same"] or d["confidence"] < min_conf:
            rec["reason"] = rec["reason"] or (
                "adjudicator: different" if not d["same"] else "below confidence floor")
            kept.append(rec)
            continue
        if state.get("dry_run"):
            merged.append(rec)
            continue
        if store.merged_ok(p["alias_id"]) or not store.weights_exists(p["alias_id"]):
            continue  # already merged earlier in this run
        moved = store.merge_weights(p["alias_id"], p["canonical_id"],
                                    d["reason"] or "llm adjudicator", d["confidence"])
        if moved or store.merged_ok(p["alias_id"]):
            merged.append(rec)
        else:
            rec["reason"] = "REFUSED by merge guard (param mismatch) — see quarantine"
            refused.append(rec)

    if not state.get("dry_run"):
        store.commit()
    store.close()
    return {"merged": merged, "kept": kept, "refused": refused}


def summarize_node(state: ResolveState) -> dict:
    return {"report": {
        "total_proposals": state.get("total_proposals", len(state.get("pairs", []))),
        "considered": len(state.get("pairs", [])),
        "merged": len(state.get("merged", [])),
        "kept": len(state.get("kept", [])),
        "refused": len(state.get("refused", [])),
        "merged_list": state.get("merged", []),
        "kept_list": state.get("kept", []),
        "refused_list": state.get("refused", []),
        "error": state.get("error"),
        "dry_run": state.get("dry_run", False),
    }}


def _route_after_adjudicate(state: ResolveState) -> str:
    return "summarize" if state.get("error") else "apply"


def build_graph():
    g = StateGraph(ResolveState)
    g.add_node("adjudicate", adjudicate_node)
    g.add_node("apply", apply_node)
    g.add_node("summarize", summarize_node)
    g.add_edge(START, "adjudicate")
    g.add_conditional_edges("adjudicate", _route_after_adjudicate,
                            {"apply": "apply", "summarize": "summarize"})
    g.add_edge("apply", "summarize")
    g.add_edge("summarize", END)
    return g.compile()


def adjudicate(
    store: Store,
    proposals: list,
    *,
    agent_model: str | None = None,
    min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
    dry_run: bool = False,
    db: str | None = None,
    max_proposals: int | None = 60,
) -> dict:
    """Review up to `max_proposals` near matches. The registry can produce tens
    of thousands; the cap keeps a real run bounded and honest about coverage."""
    total = len(proposals)
    subset = proposals[:max_proposals] if max_proposals else list(proposals)
    pairs = _pairs_from_proposals(store, subset)
    final = build_graph().invoke({
        "pairs": pairs, "total_proposals": total, "agent_model": agent_model or "",
        "db": db or getattr(store, "target", "mininfer.db"), "dry_run": dry_run,
        "min_confidence": min_confidence,
    })
    return final.get("report") or summarize_node(final)["report"]
