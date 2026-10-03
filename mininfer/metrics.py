"""Performance-metric enrichment (throughput + time-to-first-token).

Some gateways publish *performance* data that never appears in their model-list
API. Vercel's AI Gateway is the motivating case: `ai-gateway.vercel.sh/v1/models`
carries pricing, context and modalities but **no** latency/throughput, while the
gateway's public model-browser page embeds a per-model `metrics` object
(`throughput.p50TokensPerSecond`, `latency.p50TimeToFirstTokenMs`, …) in its
Next.js flight payload.

This module scrapes that page and patches the matching rows in `deployments`
(`first_token_ms`, `throughput`). It is enrichment only: it never creates a
deployment and never touches price, capability or identity, so a failed scrape
degrades the latency signal and nothing else.

Scraping a marketing page is fragile by nature. The mitigation is the same one
the rest of the registry uses: the raw HTML is snapshotted before parsing, and
`parse_metrics` is a **pure function** of that snapshot, so a markup change is a
parser fix plus a replay, never lost data.

A scraped number is a *claim*, not a fact, and it lands on a column the router
sorts on (`first_token_ms` -> `Candidate.latency_ms`, see `mi/router.py`). It is
therefore gated the same way a scraped price is: compared against what the
registry already holds, and quarantined on disagreement instead of merged. See
`validate_row` and `apply`.
"""
from __future__ import annotations

import json
import math
import re

from .fetch import Snapshot, fetch_raw
from .store import Store

VERCEL_PAGE = "https://vercel.com/ai-gateway/models"

# Next.js RSC flight payload: self.__next_f.push([1,"<escaped chunk>"])
_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)')


def _blob(html: bytes) -> str:
    """Concatenate and unescape the page's RSC chunks into one searchable string."""
    text = html.decode("utf-8", errors="replace")
    joined = "".join(_CHUNK.findall(text))
    if not joined:
        return text  # not an RSC page — fall back to the raw markup
    try:
        return joined.encode("utf-8", "surrogatepass").decode("unicode_escape", "replace")
    except Exception:
        return joined


def _objects(blob: str, key: str) -> list[dict]:
    """Every balanced `"<key>":{...}` object in the blob, as parsed JSON.

    A regex alone cannot match nested braces, so we brace-count from each hit.
    Malformed candidates are skipped rather than aborting the whole parse.
    """
    out: list[dict] = []
    for m in re.finditer(r'"%s"\s*:\s*\{' % re.escape(key), blob):
        start = m.end() - 1
        depth = 0
        i = start
        while i < len(blob):
            ch = blob[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        try:
            obj = json.loads(blob[start:i + 1])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _first(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def parse_metrics(html: bytes) -> list[dict]:
    """Extract per-model performance rows from a Vercel model-browser snapshot.

    `first_token_ms` uses the p50 TTFT (median, robust to outliers); `throughput`
    uses p50 tokens/sec. Average is the fallback when p50 is absent.
    """
    rows: list[dict] = []
    for m in _objects(_blob(html), "metrics"):
        model = m.get("model")
        if not model:
            continue
        tp = m.get("throughput") or {}
        lat = m.get("latency") or {}
        rows.append({
            "model": model,
            "provider": m.get("provider"),
            "throughput": _first(tp, "p50TokensPerSecond", "averageTokensPerSecond"),
            "first_token_ms": _first(lat, "p50TimeToFirstTokenMs",
                                     "averageTimeToFirstTokenMs"),
            "metrics": m,
        })
    return rows


def fetch_vercel(*, force: bool = False, persist: bool = True) -> Snapshot:
    """Fetch (and raw-snapshot) the Vercel model-browser page.

    `persist=False` is what `mi metrics --dry-run` passes, so the dry run reads
    the page without adding an unreferenced file to the evidence lake.
    """
    return fetch_raw("vercel_metrics", VERCEL_PAGE, ext="html", force=force,
                     persist=persist)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
# Bounds are deliberately wide. This gate exists to catch a parser drift or a unit
# change (tokens/sec published as tokens/min, milliseconds published as seconds),
# not to second-guess a real measurement. A false quarantine costs a stale number
# and one review row; a missed one costs a wrong ranking, silently.
MIN_THROUGHPUT = 0.01           # tokens/sec below this the model is not serving
MAX_THROUGHPUT = 1_000_000.0    # tokens/sec — far past any single-stream decode
MIN_FIRST_TOKEN_MS = 1.0        # sub-millisecond TTFT is a unit error, not speed
MAX_FIRST_TOKEN_MS = 600_000.0  # 10 min — past this it is a timeout, not a latency
# A metric that moves by more than this factor between scrapes is treated as a
# unit change or a different model behind the same provider id, not a real
# improvement. Same weights served under a new provider_model_id is the common
# cause; it is a rename we should see, not a 50x speedup we should believe.
MAX_CHANGE_FACTOR = 50.0

_FIELDS = (
    ("throughput", MIN_THROUGHPUT, MAX_THROUGHPUT),
    ("first_token_ms", MIN_FIRST_TOKEN_MS, MAX_FIRST_TOKEN_MS),
)


def _verdict(field: str, new: float | None, old: float | None,
             lo: float, hi: float) -> str | None:
    """Reason to refuse one metric, or None to accept it.

    `old` is what the registry holds today, or None on a first sighting — in
    which case only the physical bounds apply, because there is nothing to
    disagree with yet.
    """
    if new is None:
        return None  # nothing claimed; absence is not a claim
    if not math.isfinite(new):
        return f"{field} is not a finite number ({new})"
    if not lo <= new <= hi:
        return f"{field}={new} outside the plausible range [{lo}, {hi}]"
    if old is not None and old > 0:
        ratio = max(new / old, old / new)
        if ratio > MAX_CHANGE_FACTOR:
            return f"{field} moved {ratio:.0f}x ({old} -> {new}) — unit or ID change?"
    return None


def validate_row(row: dict, existing: dict) -> dict[str, str]:
    """Field -> reason for every metric in `row` that must not be applied.

    Pure: no store, no I/O, same shape as `router._reject`. An empty dict means
    the row is safe to apply. `existing` is the deployment's current
    `first_token_ms` / `throughput`, or `{}` for a first sighting.
    """
    out: dict[str, str] = {}
    for field, lo, hi in _FIELDS:
        reason = _verdict(field, row.get(field), existing.get(field), lo, hi)
        if reason:
            out[field] = reason
    return out


def _plan(store: Store, rows: list[dict], provider: str):
    """Decide, per row, what the gate allows and what it refuses.

    The single decision point shared by `apply` and `preview`, so `mi metrics
    --dry-run` can never disagree with what the real run would do.
    """
    for r in rows:
        deploy_id = f"{provider}:{r['model']}"
        cur = store.deployment_perf(deploy_id)
        if cur is None:
            yield {"row": r, "deploy_id": deploy_id, "missing": True,
                   "bad": {}, "updates": {}}
            continue
        bad = validate_row(r, dict(cur))
        yield {
            "row": r,
            "deploy_id": deploy_id,
            "missing": False,
            "bad": bad,
            # A refused field does not discard the rest of the row: throughput
            # and TTFT are independent claims and are judged independently.
            "updates": {f: r.get(f) for f, _, _ in _FIELDS
                        if r.get(f) is not None and f not in bad},
        }


def _tally(decisions) -> dict:
    """Fold `_plan` decisions into the counters both callers report."""
    parsed = missing = empty = 0
    refused: list[str] = []
    for d in decisions:
        parsed += 1
        if d["missing"]:
            missing += 1
            continue
        for field in d["bad"]:
            refused.append(f"{d['deploy_id']}.{field}")
        if not d["updates"]:
            empty += 1
    return {"parsed": parsed, "missing": missing, "empty": empty,
            "quarantined": len(refused), "quarantine_fields": refused}


def preview(store: Store, rows: list[dict], *, provider: str = "vercel") -> dict:
    """What `apply` would do, and why it would refuse, without writing anything."""
    return _tally(_plan(store, rows, provider))


def apply(store: Store, rows: list[dict], *, provider: str = "vercel") -> dict:
    """Patch `first_token_ms` / `throughput` on existing deployments.

    `deploy_id` is `<provider>:<model>` and the page's `model` field is exactly
    the provider model id, so the join is direct. Rows with no matching
    deployment are counted, not created.

    Two rules protect the router from this page:

    * **An absent metric never overwrites a known one.** The page carries
      throughput and TTFT independently, so a `None` on one used to blank the
      other column entirely — including the latency the router ranks on. This
      uses COALESCE, exactly as `Store.upsert_deployment` already does.
    * **A present metric that contradicts what we hold is quarantined, not
      merged** — see `validate_row`. The registry keeps the old value and
      `quarantine` records the refused one with its reason.
    """
    decisions = list(_plan(store, rows, provider))
    updated = 0
    for d in decisions:
        if d["missing"]:
            continue
        # A refusal is recorded even when the rest of the row is empty — that is
        # the whole point of refusing rather than quietly dropping.
        for field, reason in d["bad"].items():
            store.add_quarantine("metrics", d["deploy_id"], field,
                                 d["row"].get(field), f"REFUSED: {reason}")
        if not d["updates"]:
            continue
        store.set_deployment_perf(
            d["deploy_id"],
            first_token_ms=d["updates"].get("first_token_ms"),
            throughput=d["updates"].get("throughput"))
        updated += 1
    return {**_tally(decisions), "updated": updated}


def run(store: Store, *, force: bool = False) -> dict:
    """Fetch, parse, apply, and record the snapshot. Returns a summary dict."""
    snap = fetch_vercel(force=force)
    rows = parse_metrics(snap.payload)
    rep = apply(store, rows)
    store.record_snapshot(snap)
    store.commit()
    return {**rep, "url": snap.url, "sha256": snap.sha256}
