"""Ingestion agents — turn an arbitrary webpage into registry rows (Phase 5).

PLAN.md §4a: this is the *agents* layer, and only needed for non-JSON sources.
The pipeline is a LangGraph graph:

    fetch -> extract -> llm_extract -> normalize -> validate -+-> commit
                                                              +-> quarantine

Two rules are enforced at every step:

  * **Mandatory provenance.** Every fact the LLM extracts becomes an `Evidence`
    row at confidence `llm_extraction` (0.4) — low, because a webpage is a
    claim, not a measurement.
  * **A validator, not a trust.** A price scraped from a page is cross-checked
    against the registry's provider-API price for the same deployment; on
    disagreement it is quarantined instead of written (PRD §8).

The LLM is any OpenAI-compatible deployment (default: `MI_AGENT_MODEL`, else
`groq:qwen/qwen3.8-27b`), reached through `mi/execute.py`. Structured extraction
uses plain JSON + a strict local parser rather than provider-specific JSON-schema
modes, so the same graph runs on OpenRouter, Groq or Vercel unchanged.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from typing import Any, TypedDict

from bs4 import BeautifulSoup
from langgraph.graph import END, START, StateGraph

from .execute import resolve_endpoint
from .fetch import fetch_raw, utcnow
from .ingest import make_deploy, evidence, validate_prices, weights_for
from .schema import CAP_KEYS
from .store import Store

_TEXT_LIMIT = 12_000
_TABLE_LIMIT = 30
_PRICE_DISAGREE = 0.20

# Which extractor runs first. "gliner" (or "local") tries the optional local
# understanding layer before the LLM; anything else keeps the LLM-only path.
EXTRACT_BACKEND = os.environ.get("MI_EXTRACT_BACKEND", "").strip().lower()


# --------------------------------------------------------------------------- #
# graph state
# --------------------------------------------------------------------------- #


class IngestState(TypedDict, total=False):
    url: str
    source: str
    source_type: str
    agent_model: str
    dry_run: bool
    db: str
    ttl: int
    body: bytes
    is_json: bool
    text: str
    tables: list
    extraction: dict
    candidates: list[dict]
    accepted: list[dict]
    rejected: list[dict]
    report: dict
    error: str


# --------------------------------------------------------------------------- #
# fetch + extract
# --------------------------------------------------------------------------- #


def _fetch_body(url: str, source: str, ttl: int) -> bytes:
    snap = fetch_raw(source, url, cache_ttl_s=ttl)
    return snap.payload  # bytes (see fetch_raw)


def _looks_like_json(text: str) -> bool:
    s = text.lstrip()
    return s.startswith("{") or s.startswith("[")


def _html_extract(html: str) -> tuple[str, list]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header",
                     "aside", "svg", "form"]):
        tag.decompose()
    tables: list[list[list[str]]] = []
    for tbl in soup.find_all("table")[:_TABLE_LIMIT]:
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    text = soup.get_text("\n", strip=True)
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return text[:_TEXT_LIMIT], tables


def fetch_node(state: IngestState) -> dict:
    url = state["url"]
    source = state.get("source") or "add-url"
    try:
        body = _fetch_body(url, source, state.get("ttl", 6 * 3600))
    except Exception as exc:  # network / TLS / 4xx — record, do not crash the graph
        return {"error": f"{type(exc).__name__}: {exc}"}
    text = body.decode("utf-8", "replace")
    return {"body": body, "is_json": _looks_like_json(text),
            "text": text, "source": source}


def extract_node(state: IngestState) -> dict:
    if state.get("error"):
        return {}
    if state["is_json"]:
        try:
            data = json.loads(state["text"])
            return {"text": json.dumps(data, indent=2)[:_TEXT_LIMIT], "tables": []}
        except json.JSONDecodeError:
            pass  # fall through to HTML extraction on the raw text
    text, tables = _html_extract(state["text"])
    return {"text": text, "tables": tables}


# --------------------------------------------------------------------------- #
# LLM extraction
# --------------------------------------------------------------------------- #


def _agent_llm(deploy_id: str | None):
    from langchain_openai import ChatOpenAI

    deploy_id = deploy_id or os.environ.get("MI_AGENT_MODEL", "groq:qwen/qwen3.8-27b")
    ep = resolve_endpoint(deploy_id)
    if ep.error or not ep.api_key:
        raise RuntimeError(f"agent model {deploy_id!r} not callable: "
                           f"{ep.error or 'no_api_key'}")
    bundle = os.environ.get("MI_CA_BUNDLE")
    if bundle and pathlib.Path(bundle).exists():
        # langchain-openai -> openai SDK -> httpx honours SSL_CERT_FILE.
        os.environ["SSL_CERT_FILE"] = bundle

    return ChatOpenAI(model=ep.model, base_url=ep.base_url, api_key=ep.api_key,
                      temperature=0.0, timeout=120.0)


def _parse_json(s: str) -> dict | list | None:
    t = (s or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError):
            return None
    return None


_SCHEMA_HINT = """{
  "source_type": "leaderboard|pricing|model_catalog|other",
  "models": [
    {
      "model_name": "canonical name, no provider prefix",
      "provider": "hosting provider, or null",
      "provider_model_id": "id used to call it, or null",
      "family": null,
      "params_b": 30.0,
      "context_window": 32000,
      "modalities": ["text", "vision"],
      "tools": true, "structured": true, "vision": false, "reasoning": false,
      "caching": false, "audio": false,
      "price_in_usd_per_mtok": 0.05,
      "price_out_usd_per_mtok": 0.15,
      "free": false,
      "benchmarks": [{"name": "MMLU", "score": 80.0}],
      "confidence": 0.7
    }
  ],
  "notes": ""
}"""


def _extraction_prompt(text: str, tables: list, source_type: str, url: str) -> str:
    tables_json = json.dumps(tables[: _TABLE_LIMIT], ensure_ascii=False)[:6000]
    return (
        "You are extracting AI model intelligence facts from a webpage. "
        f"URL: {url}\nDeclared source type: {source_type}.\n\n"
        "Extract every AI model mentioned, with ONLY facts actually stated on the "
        "page. Never invent prices, benchmark scores, or capabilities. Use null for "
        "anything the page does not state. `price_in_usd_per_mtok` and "
        "`price_out_usd_per_mtok` are USD per million tokens. `free` is true/false/"
        "null. `benchmarks` is a list of {\"name\", \"score\"}.\n\n"
        f"Return ONLY one JSON object with this exact shape (no markdown fences, no "
        f"commentary):\n{_SCHEMA_HINT}\n\n"
        f"PAGE TEXT:\n{text}\n\nPAGE TABLES (JSON):\n{tables_json}\n"
    )


def _extract_facts(text: str, tables: list, source_type: str, url: str,
                   agent_model: str | None) -> dict:
    llm = _agent_llm(agent_model)
    resp = llm.invoke(_extraction_prompt(text, tables, source_type, url))
    content = resp.content if hasattr(resp, "content") else str(resp)
    data = _parse_json(content)
    if not isinstance(data, dict):
        raise ValueError("agent did not return a JSON object")
    return data


def understanding_node(state: IngestState) -> dict:
    """Cheap local extraction, before the LLM node. Opt-in via `MI_EXTRACT_BACKEND`.

    When the local model returns at least one *named* model record, `extraction`
    is set and `llm_extract_node` becomes a no-op for this run. When it returns
    nothing — a page with no models, or the extra not installed — it sets
    nothing and the LLM path runs exactly as before. That asymmetry is the point:
    enabling this can only remove work, never silently drop a fact.

    The records it produces are the *same shape* `_SCHEMA_HINT` describes, so the
    existing normalize/validate/quarantine pipeline — including the price
    cross-check that keeps a scraped claim from overwriting a provider-API price
    — is unchanged. A local extractor is a cheaper source of evidence, not a new
    authority.
    """
    if state.get("error") or state.get("extraction"):
        return {}
    if EXTRACT_BACKEND not in ("gliner", "local", "understanding"):
        return {}
    from . import understanding

    if not understanding.available():
        return {}
    try:
        extraction = understanding.extract_models(state["text"])
    except Exception:
        return {}
    if not extraction:
        return {}
    return {
        "extraction": extraction,
        "source_type": extraction.get("source_type") or state.get("source_type", "auto"),
    }


def llm_extract_node(state: IngestState) -> dict:
    if state.get("error") or state.get("extraction"):
        return {}
    try:
        extraction = _extract_facts(state["text"], state["tables"],
                                    state.get("source_type", "auto"),
                                    state["url"], state.get("agent_model"))
        return {"extraction": extraction,
                "source_type": extraction.get("source_type") or state.get("source_type", "auto")}
    except Exception as exc:
        return {"error": f"llm_extract: {type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# normalize
# --------------------------------------------------------------------------- #


def _num(v) -> float | None:
    try:
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    except (TypeError, ValueError):
        return None


def _int_or_none(v) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _normalize_fact(m: dict, source: str, url: str, fetched_at: str) -> dict | None:
    name = (m.get("model_name") or "").strip()
    if not name:
        return None
    provider = (m.get("provider") or "web").strip().lower() or "web"
    pmid = (m.get("provider_model_id") or name).strip()
    pin = _num(m.get("price_in_usd_per_mtok"))
    pout = _num(m.get("price_out_usd_per_mtok"))
    free = m.get("free")

    benchmarks: dict[str, float] = {}
    for b in m.get("benchmarks") or []:
        if isinstance(b, dict) and b.get("name") and _num(b.get("score")) is not None:
            benchmarks[str(b["name"])] = float(b["score"])

    caps: dict[str, bool | None] = {}
    for k in CAP_KEYS:
        v = m.get(k)
        caps[k] = None if v is None else bool(v)

    if free is True and pin is None and pout is None:
        pin, pout = 0.0, 0.0  # "free" with no stated price -> list price zero

    modalities = m.get("modalities") or []
    if isinstance(modalities, str):
        modalities = [modalities]

    weights = weights_for(name, None, modalities=modalities,
                           source=source, url=url, fetched_at=fetched_at,
                           benchmark=benchmarks, benchmark_source=source)
    deploy = make_deploy(
        provider, pmid, weights.weights_id, source=source, source_url=url,
        ctx=_int_or_none(m.get("context_window")),
        pin=pin, pout=pout, caps=caps, limits_confirmed=False,
    )
    price_error = validate_prices(provider, pmid, pin, pout)
    return {
        "name": name, "provider": provider, "provider_model_id": pmid,
        "weights": weights, "deployment": deploy,
        "confidence": float(m.get("confidence") or 0.5),
        "price_error": price_error,
        "price_in": pin, "price_out": pout, "free": free,
        "benchmarks": benchmarks,
        "modalities": list(modalities),
    }


def normalize_node(state: IngestState) -> dict:
    if state.get("error"):
        return {}
    fetched_at = utcnow()
    candidates = []
    for m in state["extraction"].get("models", []):
        c = _normalize_fact(m, state["source"], state["url"], fetched_at)
        if c:
            candidates.append(c)
    return {"candidates": candidates}


# --------------------------------------------------------------------------- #
# validate (the validator agent's deterministic half)
# --------------------------------------------------------------------------- #


def _cross_check(store: Store, c: dict) -> list[tuple]:
    """Compare a scraped price/free claim against the registry's provider-API
    price. Disagreement is quarantined, not silently merged."""
    out: list[tuple] = []
    row = store.deployment_prices(c["deployment"].deploy_id)
    if row is None:
        return out
    for label, got, reg in (("price_in", c["price_in"], row["price_in"]),
                            ("price_out", c["price_out"], row["price_out"])):
        if got is None or reg is None:
            continue
        if abs(got - reg) > _PRICE_DISAGREE * max(1e-9, abs(reg)):
            out.append(("add-url", c["deployment"].deploy_id, label, got,
                        f"disagrees with provider_api price {reg:.6f}"))
    reg_free = bool(row["zero_price"] or row["free_variant"]
                    or row["subscription"] or row["trial_credits"])
    if c["free"] is True and not reg_free:
        out.append(("add-url", c["deployment"].deploy_id, "free", True,
                    "page says free but registry has a positive price"))
    return out


def validate_node(state: IngestState) -> dict:
    if state.get("error"):
        return {}
    store = Store(state.get("db", "mininfer.db"))   # may be a path or a DSN
    accepted, rejected = [], []
    for c in state["candidates"]:
        problems = []
        if c["price_error"]:
            problems.append(c["price_error"])
        problems += [p[4] for p in _cross_check(store, c)]
        if problems:
            c["reject_reason"] = "; ".join(problems)
            rejected.append(c)
        else:
            accepted.append(c)
    store.close()
    return {"accepted": accepted, "rejected": rejected}


# --------------------------------------------------------------------------- #
# commit / quarantine
# --------------------------------------------------------------------------- #


def _quarantine_rows(c: dict) -> list[tuple]:
    return [("add-url", c["deployment"].deploy_id, "rejected",
             c.get("reject_reason"), c.get("reject_reason", "validation failed"))]


def _write_candidate(store: Store, c: dict, dry_run: bool) -> None:
    if dry_run:
        return
    w, d = c["weights"], c["deployment"]
    fetched = utcnow()
    w_evidence = evidence("weights", w.weights_id, {
        "display_name": w.display_name, "benchmark": w.benchmark or None,
        "modalities": list(w.modalities) or None,
    }, c["provider"], d.source_url, fetched, "llm_extraction")
    d_evidence = evidence("deployment", d.deploy_id, {
        "price_in": d.price_in, "price_out": d.price_out,
        "context_window": d.context_window, "zero_price": d.zero_price or None,
    }, c["provider"], d.source_url, fetched, "llm_extraction")
    store.upsert_weights(w, w_evidence)
    store.upsert_deployment(d, d_evidence)


def _write_quarantines(store: Store, rejected: list[dict], dry_run: bool) -> None:
    if dry_run:
        return
    for c in rejected:
        for source, eid, field, value, reason in _quarantine_rows(c):
            store.add_quarantine(source, eid, field, value, reason)


def _report(state: IngestState) -> dict:
    return {
        "url": state["url"],
        "source": state.get("source"),
        "source_type": state.get("source_type", "auto"),
        "models_found": len(state.get("candidates", [])),
        "accepted": len(state.get("accepted", [])),
        "quarantined": len(state.get("rejected", [])),
        "accepted_deployments": [c["deployment"].deploy_id for c in state.get("accepted", [])],
        "rejected": [
            {"deploy_id": c["deployment"].deploy_id, "reason": c.get("reject_reason")}
            for c in state.get("rejected", [])
        ],
        "error": state.get("error"),
        "dry_run": state.get("dry_run", False),
    }


def commit_node(state: IngestState) -> dict:
    store = Store(state.get("db", "mininfer.db"))   # may be a path or a DSN
    for c in state.get("accepted", []):
        _write_candidate(store, c, state.get("dry_run", False))
    _write_quarantines(store, state.get("rejected", []), state.get("dry_run", False))
    store.commit()
    store.close()
    return {"report": _report(state)}


quarantine_node = commit_node


# --------------------------------------------------------------------------- #
# graph
# --------------------------------------------------------------------------- #


def _route_after_validate(state: IngestState) -> str:
    return "commit" if state.get("accepted") else "quarantine"


def build_graph():
    g = StateGraph(IngestState)
    g.add_node("fetch", fetch_node)
    g.add_node("extract", extract_node)
    g.add_node("understanding", understanding_node)
    g.add_node("llm_extract", llm_extract_node)
    g.add_node("normalize", normalize_node)
    g.add_node("validate", validate_node)
    g.add_node("commit", commit_node)
    g.add_node("quarantine", quarantine_node)
    g.add_edge(START, "fetch")
    g.add_edge("fetch", "extract")
    g.add_edge("extract", "understanding")
    g.add_edge("understanding", "llm_extract")
    g.add_edge("llm_extract", "normalize")
    g.add_edge("normalize", "validate")
    g.add_conditional_edges("validate", _route_after_validate,
                            {"commit": "commit", "quarantine": "quarantine"})
    g.add_edge("commit", END)
    g.add_edge("quarantine", END)
    return g.compile()


def add_url(
    url: str,
    *,
    source: str | None = None,
    source_type: str = "auto",
    agent_model: str | None = None,
    db: str = "mininfer.db",
    dry_run: bool = False,
    ttl: int = 6 * 3600,
) -> dict:
    graph = build_graph()
    state: IngestState = {
        "url": url, "source": source or "add-url", "source_type": source_type,
        "agent_model": agent_model or "", "dry_run": dry_run, "db": db, "ttl": ttl,
    }
    final = graph.invoke(state)
    if final.get("error") and "report" not in final:
        return {"url": url, "source": source or "add-url", "source_type": source_type,
                "models_found": 0, "accepted": 0, "quarantined": 0,
                "accepted_deployments": [], "rejected": [], "error": final["error"],
                "dry_run": dry_run}
    return final.get("report") or _report(final)
